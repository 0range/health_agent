"""Conservative metadata-only classifier for medical attachments."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import PurePath

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
        r"\b(?:анализ\w*|лаборатор\w*|заключени\w*|исследовани\w*)\b",
        r"\b(?:обследовани\w*|медицин\w*|клиник\w*|врач\w*|рецепт\w*)\b",
        r"\b(?:мрт|кт|узи|рентген\w*|выписк\w*|госпитал\w*)\b",
    )
)


@dataclass(frozen=True, slots=True)
class Classification:
    decision: str
    effective_mime_type: str | None
    reason_code: str


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
