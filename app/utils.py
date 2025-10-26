"""Utility helpers for templating, email delivery, and rate limiting."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape
from ratelimit import limits, sleep_and_retry
from sendgrid import SendGridAPIClient
from sendgrid.helpers.mail import Email, Mail
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
MPS_LIMIT = int(os.getenv("MPS_LIMIT", "60"))
WINDOW_SECONDS = int(os.getenv("WINDOW_SECONDS", "60"))
SENDGRID_API_KEY = os.getenv("SENDGRID_API_KEY")
FROM_EMAIL = os.getenv("FROM_EMAIL", "funding@rhfunding.io")
FROM_NAME = os.getenv("FROM_NAME", "RedHat Funding")
REPLY_TO_EMAIL = os.getenv("REPLY_TO_EMAIL", FROM_EMAIL)
BUSINESS_NAME = os.getenv("BUSINESS_NAME", "RedHat Funding")
BUSINESS_ADDRESS = os.getenv(
    "BUSINESS_ADDRESS", "123 Main St, Fort Lauderdale, FL 33301, USA"
)
OPTOUT_MODE = os.getenv("OPTOUT_MODE", "link")
OPTOUT_LINK = os.getenv("OPTOUT_LINK", "https://rhfunding.io/unsubscribe")

_env: Optional[Environment] = None


def get_template_env() -> Environment:
    global _env
    if _env is None:
        _env = Environment(
            loader=FileSystemLoader(str(TEMPLATES_DIR)),
            autoescape=select_autoescape(["html", "xml"]),
        )
    return _env


def render_email(template_name: str, context: Dict[str, Any]) -> str:
    env = get_template_env()
    template = env.get_template(template_name)

    context_with_defaults = dict(context)
    context_with_defaults.setdefault("business_name", BUSINESS_NAME)
    context_with_defaults.setdefault("business_address", BUSINESS_ADDRESS)
    context_with_defaults.setdefault("from_name", FROM_NAME)
    context_with_defaults.setdefault("optout_mode", OPTOUT_MODE)

    base_unsubscribe_url = context_with_defaults.get("unsubscribe_url", OPTOUT_LINK)
    email = context.get("email", "")
    if email:
        separator = "&" if "?" in base_unsubscribe_url else "?"
        unsubscribe_url = f"{base_unsubscribe_url}{separator}email={email}"
    else:
        unsubscribe_url = base_unsubscribe_url
    context_with_defaults["unsubscribe_url"] = unsubscribe_url

    return template.render(**context_with_defaults)


@dataclass
class EmailPayload:
    to_email: str
    subject: str
    html_content: str


def _build_mail(payload: EmailPayload) -> Mail:
    message = Mail(
        from_email=Email(FROM_EMAIL, FROM_NAME),
        to_emails=Email(payload.to_email),
        subject=payload.subject,
        html_content=payload.html_content,
    )
    if REPLY_TO_EMAIL:
        message.reply_to = Email(REPLY_TO_EMAIL)
    return message


def _dispatch_sendgrid(message: Mail) -> None:
    if not SENDGRID_API_KEY:
        raise RuntimeError("SENDGRID_API_KEY is not configured")
    client = SendGridAPIClient(SENDGRID_API_KEY)
    client.send(message)


@retry(wait=wait_exponential(multiplier=1, min=2, max=10), stop=stop_after_attempt(3))
def send_email(payload: EmailPayload) -> None:
    message = _build_mail(payload)
    _dispatch_sendgrid(message)


@sleep_and_retry
@limits(calls=MPS_LIMIT, period=WINDOW_SECONDS)
def send_email_with_fallback(payload: EmailPayload) -> bool:
    try:
        send_email(payload)
        return True
    except RetryError as exc:  # pragma: no cover - informational logging
        logger.error("Failed to send email after retries: %s", exc)
        return False
    except Exception as exc:  # pragma: no cover - unexpected
        logger.exception("Unexpected error while sending email: %s", exc)
        return False
