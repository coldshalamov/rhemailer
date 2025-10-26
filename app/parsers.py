"""Document parsing utilities for the prepare workflow."""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
from pdf2image import convert_from_path  # type: ignore
from pdfminer.high_level import extract_text
from pytesseract import image_to_string

FINANCIAL_PATTERNS = {
    "avg_deposits": re.compile(r"avg(?:erage)?\s+deposits?\s*[:$]?\s*([\d,.,]+)", re.I),
    "nsf_count": re.compile(r"nsf\s*(?:count)?\s*[:]?\s*(\d+)", re.I),
    "monthly_revenue": re.compile(r"monthly\s+revenue\s*[:$]?\s*([\d,.,]+)", re.I),
}

PII_PATTERNS = [
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),  # SSN
    re.compile(r"\b\d{4}\s\d{4}\s\d{4}\s\d{4}\b"),  # Credit card
    re.compile(r"\b\d{16}\b"),
]

logger = logging.getLogger(__name__)


@dataclass
class ParsedDocument:
    metrics: Dict[str, Any]
    leads: List[Dict[str, Any]]


def _normalize_currency(value: str) -> Optional[float]:
    try:
        cleaned = value.replace(",", "").replace("$", "").strip()
        return float(cleaned)
    except (AttributeError, ValueError):
        return None


def _extract_metrics(text: str) -> Dict[str, Any]:
    metrics: Dict[str, Any] = {}
    for key, pattern in FINANCIAL_PATTERNS.items():
        match = pattern.search(text)
        if not match:
            continue
        if "count" in key:
            metrics[key] = int(match.group(1))
        else:
            value = _normalize_currency(match.group(1))
            if value is not None:
                metrics[key] = value
    return metrics


def _redact_pii(text: str) -> str:
    redacted = text
    for pattern in PII_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def _parse_pdf_text(pdf_path: Path) -> str:
    try:
        text = extract_text(str(pdf_path))
    except Exception as exc:  # pragma: no cover - pdfminer edge cases
        logger.warning("Failed to extract text from PDF %s: %s", pdf_path, exc)
        text = ""

    if text.strip():
        return text

    try:
        images = convert_from_path(str(pdf_path))
    except OSError as exc:  # pragma: no cover - poppler rendering issues
        logger.warning("Unable to render PDF %s for OCR: %s", pdf_path, exc)
        return text

    ocr_chunks: List[str] = []
    for index, image in enumerate(images):
        try:
            ocr_chunks.append(image_to_string(image))
        except Exception as exc:  # pragma: no cover - pytesseract runtime errors
            logger.warning("Failed OCR on page %s of %s: %s", index + 1, pdf_path, exc)
    ocr_text = "\n".join(chunk for chunk in ocr_chunks if chunk.strip())
    return ocr_text or text


def parse_pdf(pdf_path: Path) -> Dict[str, Any]:
    text = _parse_pdf_text(pdf_path)
    sanitized_text = _redact_pii(text)
    metrics = _extract_metrics(sanitized_text)
    return {"metrics": metrics, "raw_text": sanitized_text[:2000]}


def parse_csv(csv_path: Path) -> List[Dict[str, Any]]:
    df = pd.read_csv(csv_path)
    df.columns = [col.strip().lower() for col in df.columns]
    leads: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        lead = {
            "company": row.get("company") or row.get("business") or "Unknown",
            "contact_name": row.get("contact") or row.get("name"),
            "email": str(row.get("email", "")).strip(),
            "phone": str(row.get("phone", "")).strip(),
            "avg_deposits": row.get("avg_deposits"),
            "nsf_count": row.get("nsf") or row.get("nsf_count"),
        }
        leads.append({k: v for k, v in lead.items() if pd.notna(v) and v != "nan"})
    return leads


def handle_uploads(files: Iterable[Any]) -> ParsedDocument:
    metrics: Dict[str, Any] = {}
    leads: List[Dict[str, Any]] = []

    for upload in files:
        suffix = Path(upload.filename).suffix.lower()
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(upload.file.read())
            tmp_path = Path(tmp.name)
        try:
            if suffix in {".csv"}:
                leads.extend(parse_csv(tmp_path))
            elif suffix in {".pdf"}:
                parsed = parse_pdf(tmp_path)
                metrics.update(parsed.get("metrics", {}))
            else:
                raise ValueError(f"Unsupported file type: {suffix}")
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return ParsedDocument(metrics=metrics, leads=leads)


def redact_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    redacted = json.loads(json.dumps(payload))
    for lead in redacted.get("leads", []):
        if "email" in lead:
            lead["email"] = _mask_email(lead["email"])
        if "phone" in lead:
            lead["phone"] = _mask_phone(lead["phone"])
    return redacted


def _mask_email(email: str) -> str:
    if "@" not in email:
        return email
    name, domain = email.split("@", 1)
    if len(name) <= 2:
        return "*" * len(name) + "@" + domain
    return name[0] + "***" + name[-1] + "@" + domain


def _mask_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) < 4:
        return "***"
    return f"***-***-{digits[-4:]}"
