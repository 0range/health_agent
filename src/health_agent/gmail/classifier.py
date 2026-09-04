"""Conservative first-stage classifier for Gmail medical candidates."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from html import unescape
from pathlib import PurePath

from health_agent.gmail.preparation import (
    InvalidAttachmentEncoding,
    iter_base64url_chunks,
)
from health_agent.gmail.types import GmailMessage, GmailPart

_SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/heic",
    "image/heif",
    "image/jpeg",
    "image/png",
    "image/tiff",
    "image/webp",
}
_SUFFIX_MIME_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".jpeg": "image/jpeg",
    ".jpg": "image/jpeg",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}
_MEDICAL_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\b(?:blood|lab(?:oratory)?|medical|clinic|doctor|physician)\b",
        r"\b(?:mri|ultrasound|x[ -]?ray|radiology|prescription|discharge)\b",
        r"\b(?:appointment|consultation|follow[ -]?up|check[ -]?up)\b",
        r"\b(?:анализ\w*|лаборатор\w*|заключени\w*|исследовани\w*)\b",
        r"\b(?:обследовани\w*|медицин\w*|клиник\w*|врач\w*|рецепт\w*)\b",
        r"\b(?:мрт|кт|узи|рентген\w*|выписк\w*|госпитал\w*)\b",
        r"\b(?:при[её]м\w*|консультаци\w*|осмотр\w*|чекап\w*)\b",
    )
)
_APPOINTMENT = re.compile(
    r"\b(?:appointment|consultation|visit|doctor|physician|при[её]м\w*|"
    r"консультаци\w*|визит\w*|врач\w*)\b",
    re.IGNORECASE,
)
_HTML_TAG = re.compile(r"<[^>]+>")
_MAX_BODY_CLASSIFICATION_BYTES = 256 * 1024


@dataclass(frozen=True, slots=True)
class Classification:
    decision: str
    effective_mime_type: str | None
    reason_code: str


def classify_message(message: GmailMessage) -> Classification:
    """Recognize a body-only medical appointment without retaining body text."""
    subject = _normalize(message.subject)
    if _APPOINTMENT.search(subject):
        return Classification("appointment", None, "appointment_subject")
    remaining = _MAX_BODY_CLASSIFICATION_BYTES
    for part in _walk_text_parts(message.payload):
        if part.body_data is None or remaining <= 0:
            continue
        try:
            chunks: list[bytes] = []
            for chunk in iter_base64url_chunks(part.body_data):
                chunks.append(chunk[:remaining])
                remaining -= min(len(chunk), remaining)
                if remaining == 0:
                    break
        except InvalidAttachmentEncoding:
            continue
        raw = b"".join(chunks)
        text = raw.decode("utf-8", errors="ignore")
        if part.mime_type == "text/html":
            text = unescape(_HTML_TAG.sub(" ", text))
        if _APPOINTMENT.search(_normalize(text)):
            return Classification("appointment", None, "appointment_body")
    return Classification("ignored", None, "no_message_signal")


def classify_attachment(
    message: GmailMessage,
    part: GmailPart,
    trusted_senders: tuple[str, ...],
) -> Classification:
    """Classify from headers and filename only; message bodies are never inspected."""
    mime_type = _effective_mime_type(part)
    if mime_type is None:
        return Classification("ignored", None, "unsupported_mime")
    if not part.filename.strip():
        return Classification("ignored", mime_type, "missing_filename")
    if (
        mime_type.startswith("image/")
        and part.disposition is not None
        and part.disposition.casefold().startswith("inline")
    ):
        return Classification("ignored", mime_type, "inline_image")

    sender = message.sender.casefold()
    if sender and sender in trusted_senders:
        return Classification("suspected_medical", mime_type, "trusted_sender")
    searchable = f"{_normalize(part.filename)} {_normalize(message.subject)}"
    if any(pattern.search(searchable) for pattern in _MEDICAL_PATTERNS):
        return Classification("suspected_medical", mime_type, "medical_metadata")
    return Classification("ambiguous", mime_type, "insufficient_medical_signal")


def _effective_mime_type(part: GmailPart) -> str | None:
    if part.mime_type in _SUPPORTED_MIME_TYPES:
        return part.mime_type
    if part.mime_type in {"application/octet-stream", "binary/octet-stream"}:
        return _SUFFIX_MIME_TYPES.get(PurePath(part.filename).suffix.casefold())
    return None


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().replace("_", " ")


def _walk_text_parts(part: GmailPart) -> tuple[GmailPart, ...]:
    found: list[GmailPart] = []
    if part.mime_type in {"text/plain", "text/html"} and not part.filename:
        found.append(part)
    for child in part.children:
        found.extend(_walk_text_parts(child))
    return tuple(found)
