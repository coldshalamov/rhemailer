"""FastAPI application entrypoint for rh-emailer."""

import logging
import os
import uuid
from typing import Any, Dict, List, Optional, Literal

import json

from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, EmailStr, Field
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

app = FastAPI(title="rh-emailer", version="1.1.2")
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
    to_email: EmailStr
    subject: str = "Funding Offer"
    body_html: str
    tone: Literal["conservative", "assertive"] = "conservative"
    dry_run: bool = True


class DirectSendResponse(BaseModel):
    sent: bool
    id: str
    reason: Optional[str] = None


DirectSendRequest.model_rebuild()
DirectSendResponse.model_rebuild()

class StatusResponse(BaseModel):
    job_id: str
    status: str
    payload: Optional[Dict[str, Any]]
    result: Optional[Dict[str, Any]]


class SuppressionResponse(BaseModel):
    email: str
    suppressed: bool


class UnsubscribeRequest(BaseModel):
    email: EmailStr


def auth(credentials: HTTPAuthorizationCredentials = Depends(security)) -> None:
    if not API_BEARER_TOKEN:
        return
    if credentials is None or credentials.credentials != API_BEARER_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> Dict[str, str]:
    return {"status": "ok", "version": app.version if hasattr(app, "version") else "unknown"}


@app.post("/prepare", response_model=PrepareResponse)
def prepare_endpoint(
    files: List[UploadFile] = File(...),
    tone: str = Form(DEFAULT_TONE),
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


@app.post("/direct_send", response_model=DirectSendResponse)
@sleep_and_retry
@limits(calls=MPS_LIMIT, period=WINDOW_SECONDS)
def direct_send_endpoint(
    # Preferred path: JSON body
    payload: Optional[DirectSendRequest] = Body(None),
    # Legacy path: ?payload=<JSON>
    payload_q: Optional[str] = Query(None, alias="payload"),
    _: None = Depends(auth),
) -> DirectSendResponse:
    message_id = str(uuid.uuid4())

    # Accept either JSON body or legacy ?payload=
    if payload is None:
        if not payload_q:
            raise HTTPException(
                status_code=422,
                detail="Missing request body (DirectSendRequest) or legacy ?payload=<json> query parameter."
            )
        try:
            # Try pydantic v2, then v1
            try:
                payload = DirectSendRequest.model_validate_json(payload_q)  # pydantic v2
            except AttributeError:
                payload = DirectSendRequest.parse_raw(payload_q)            # pydantic v1
        except Exception as e:
            # Clarify if it's JSON vs schema
            try:
                json.loads(payload_q)
            except json.JSONDecodeError as decode_error:
                raise HTTPException(status_code=422, detail=f"Invalid JSON in query param 'payload': {decode_error}") from decode_error
            raise HTTPException(status_code=422, detail=f"Invalid DirectSendRequest in query param 'payload': {e}") from e

    # Body must not be empty
    if not payload.body_html or not str(payload.body_html).strip():
        raise HTTPException(status_code=422, detail="body_html is required and cannot be empty.")

    # Suppression pre-check
    with db.get_session() as session:
        if db.is_suppressed(session, str(payload.to_email)):
            return DirectSendResponse(sent=False, id=message_id, reason="suppressed")

    email_payload = EmailPayload(
        to_email=str(payload.to_email),
        subject=payload.subject,
        html_content=payload.body_html,  # footer should already be present per agent
    )

    # Dry-run behavior: indicate acceptance without delivery
    if payload.dry_run:
        return DirectSendResponse(sent=True, id=message_id)

    try:
        sent_ok = send_email_with_fallback(email_payload)
    except Exception:
        logger.exception("direct_send send failed")
        return DirectSendResponse(sent=False, id=message_id, reason="send failed")

    return DirectSendResponse(sent=bool(sent_ok), id=message_id, reason=None if sent_ok else "send failed")


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


@app.post("/unsubscribe", response_model=SuppressionResponse)
def unsubscribe_post(
    req: UnsubscribeRequest, _: None = Depends(auth)
) -> SuppressionResponse:
    with db.get_session() as session:
        added = db.add_to_suppression(session, str(req.email))
    return SuppressionResponse(email=str(req.email), suppressed=added)


def _resolve_template(tone: str) -> str:
    if tone not in {"conservative", "assertive"}:
        logger.warning("Unknown tone '%s', defaulting to conservative", tone)
        tone = DEFAULT_TONE
    return f"{tone}.html.j2"
