"""FastAPI application entrypoint for rh-emailer."""

from __future__ import annotations

import logging
import os
from uuid import uuid4
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from ratelimit import limits, sleep_and_retry

from . import db
from .parsers import ParsedDocument, handle_uploads, redact_payload
from .utils import (
    BUSINESS_ADDRESS,
    OPTOUT_LINK,
    EmailPayload,
    MPS_LIMIT,
    WINDOW_SECONDS,
    render_email,
    send_email_with_fallback,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

API_BEARER_TOKEN = os.getenv("API_BEARER_TOKEN")
DEFAULT_TONE = "conservative"

app = FastAPI(title="rh-emailer", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

db.init_db()

security = HTTPBearer(auto_error=False)


class PrepareResponse(BaseModel):
    prepare_id: str
    count: int
    preview: List[Dict[str, Any]]
    metrics: Dict[str, Any]


class SendRequest(BaseModel):
    prepare_id: str = Field(..., description="Identifier returned from /prepare")
    dry_run: bool = Field(default=True, description="Only queue without sending")
    tone: Optional[str] = Field(default=None, description="Override tone template")


class SendResponse(BaseModel):
    job_id: str
    queued: bool
    summary: Dict[str, Any]


class DirectSendRequest(BaseModel):
    to_email: str = Field(..., description="Recipient email address")
    subject: str = Field(default="Funding Offer", description="Email subject line")
    body_html: str = Field(..., description="HTML body content to embed")
    tone: Optional[str] = Field(
        default=DEFAULT_TONE,
        description="Tone template to wrap the email body",
    )
    dry_run: bool = Field(
        default=True,
        description="Simulate the send without dispatching the email",
    )


class DirectSendResponse(BaseModel):
    id: str
    sent: bool
    reason: Optional[str] = Field(default=None, description="Reason email was not sent")


class StatusResponse(BaseModel):
    job_id: str
    status: str
    payload: Optional[Dict[str, Any]]
    result: Optional[Dict[str, Any]]


class SuppressionResponse(BaseModel):
    email: str
    suppressed: bool


def auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if not API_BEARER_TOKEN:
        return
    if credentials is None or credentials.credentials != API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok"}


@app.post("/prepare", response_model=PrepareResponse)
def prepare_endpoint(
    files: List[UploadFile] = File(...),
    tone: str = DEFAULT_TONE,
    _: None = Depends(auth),
) -> PrepareResponse:
    if not files:
        raise HTTPException(status_code=400, detail="No files uploaded")

    tone = tone.lower()
    template_name = _resolve_template(tone)

    parsed: ParsedDocument = handle_uploads(files)
    payload = {
        "tone": tone,
        "metrics": parsed.metrics,
        "leads": parsed.leads,
    }

    preview_entries = _build_preview(parsed.leads, parsed.metrics, template_name)

    with db.get_session() as session:
        job = db.create_job(session, payload)
        db.update_job_status(session, job.id, "prepared")
        prepare_id = job.id

    return PrepareResponse(
        prepare_id=prepare_id,
        count=len(parsed.leads),
        preview=preview_entries,
        metrics=parsed.metrics,
    )


def _build_preview(
    leads: List[Dict[str, Any]], metrics: Dict[str, Any], template_name: str
) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for lead in leads[:10]:
        context = {**metrics, **lead}
        html = render_email(template_name, context)
        preview.append({
            "lead": redact_payload({"leads": [lead]}).get("leads", [lead])[0],
            "email_html": html,
        })
    return preview


@app.post("/send", response_model=SendResponse)
def send_endpoint(request: SendRequest, _: None = Depends(auth)) -> SendResponse:
    with db.get_session() as session:
        prepare_job = db.get_job(session, request.prepare_id)
        if prepare_job is None:
            raise HTTPException(status_code=404, detail="prepare_id not found")
        payload = prepare_job.get_payload() or {}
        leads = payload.get("leads", [])
        if not leads:
            raise HTTPException(status_code=400, detail="No leads available to send")
        tone = (request.tone or payload.get("tone") or DEFAULT_TONE).lower()
        template_name = _resolve_template(tone)

        send_job_payload = {
            "prepare_id": request.prepare_id,
            "tone": tone,
            "dry_run": request.dry_run,
        }
        send_job = db.create_job(session, send_job_payload)
        db.update_job_status(session, send_job.id, "queued")
        job_id = send_job.id

    if request.dry_run:
        summary = {
            "message": "Dry run completed; no emails sent",
            "recipients": len(leads),
        }
        with db.get_session() as session:
            db.update_job_status(session, job_id, "dry_run", result=summary)
        return SendResponse(job_id=job_id, queued=False, summary=summary)

    summary = _process_sends(job_id, leads, payload.get("metrics", {}), template_name)
    return SendResponse(job_id=job_id, queued=True, summary=summary)


def _process_sends(
    job_id: str,
    leads: List[Dict[str, Any]],
    metrics: Dict[str, Any],
    template_name: str,
) -> Dict[str, Any]:
    sent = 0
    skipped = 0
    suppressed = 0
    failures: List[str] = []

    with db.get_session() as session:
        for lead in leads:
            email = lead.get("email")
            if not email:
                skipped += 1
                continue
            if db.is_suppressed(session, email):
                suppressed += 1
                continue
            context = {**metrics, **lead}
            html = render_email(template_name, context)
            payload = EmailPayload(
                to_email=email,
                subject="Funding options tailored for your business",
                html_content=html,
            )
            success = send_email_with_fallback(payload)
            if success:
                sent += 1
            else:
                failures.append(email)

    summary = {
        "sent": sent,
        "skipped_missing_contact": skipped,
        "suppressed": suppressed,
        "failures": failures,
    }

    with db.get_session() as session:
        status = "completed" if not failures else "completed_with_errors"
        db.update_job_status(session, job_id, status, result=summary)

    return summary


def _build_direct_send_html(request: DirectSendRequest) -> str:
    tone = (request.tone or DEFAULT_TONE).lower()
    template_name = _resolve_template(tone)
    context = {
        "contact_name": "User",
        "avg_deposits": 0,
        "nsf_count": 0,
        "email": request.to_email,
        "custom_body_html": request.body_html,
        "business_address": BUSINESS_ADDRESS,
        "unsubscribe_url": OPTOUT_LINK,
    }
    return render_email(template_name, context)


@app.post("/direct_send", response_model=DirectSendResponse)
@sleep_and_retry
@limits(calls=MPS_LIMIT, period=WINDOW_SECONDS)
def direct_send_endpoint(
    request: DirectSendRequest, _: None = Depends(auth)
) -> DirectSendResponse:
    message_id = str(uuid4())

    with db.get_session() as session:
        if db.is_suppressed(session, request.to_email):
            return DirectSendResponse(id=message_id, sent=False, reason="suppressed")

    rendered_html = _build_direct_send_html(request)

    if request.dry_run:
        return DirectSendResponse(id=message_id, sent=False)

    payload = EmailPayload(
        to_email=request.to_email,
        subject=request.subject,
        html_content=rendered_html,
    )

    sent = send_email_with_fallback(payload)
    return DirectSendResponse(id=message_id, sent=sent)


@app.get("/status/{job_id}", response_model=StatusResponse)
def status_endpoint(job_id: str, _: None = Depends(auth)) -> StatusResponse:
    with db.get_session() as session:
        job = db.get_job(session, job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return StatusResponse(
            job_id=job.id,
            status=job.status,
            payload=job.get_payload(),
            result=job.get_result(),
        )


@app.get("/unsubscribe", response_model=SuppressionResponse)
def unsubscribe(email: str) -> SuppressionResponse:
    with db.get_session() as session:
        added = db.add_to_suppression(session, email)
    return SuppressionResponse(email=email, suppressed=added)


def _resolve_template(tone: str) -> str:
    if tone not in {"conservative", "assertive"}:
        logger.warning("Unknown tone '%s', defaulting to conservative", tone)
        tone = DEFAULT_TONE
    return f"{tone}.html.j2"
