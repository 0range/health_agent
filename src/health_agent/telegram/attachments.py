"""Private pre-inbox attachment staging and signature validation."""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from health_agent.telegram.api import MAX_DOWNLOAD_BYTES, TelegramAPIError
from health_agent.telegram.stores import private_directory
from health_agent.telegram.types import AttachmentKind


class AttachmentValidationError(TelegramAPIError):
    def __init__(self, safe_error_code: str) -> None:
        super().__init__(safe_error_code)


@dataclass(frozen=True, slots=True)
class StagedAttachment:
    path: Path
    sha256: str
    size_bytes: int
    media_type: str

    def chunks(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        with self.path.open("rb") as handle:
            while chunk := handle.read(chunk_size):
                yield chunk


@contextmanager
def stage_attachment(
    root: Path,
    chunks: Iterable[bytes],
    *,
    kind: AttachmentKind,
    declared_mime_type: str,
    declared_size: int | None,
    remote_size: int | None,
) -> Iterator[StagedAttachment]:
    """Fully validate untrusted bytes before yielding them to a committing inbox."""
    directory = private_directory(root)
    descriptor, name = tempfile.mkstemp(
        dir=directory, prefix="attachment-", suffix=".part"
    )
    path = Path(name)
    digest = hashlib.sha256()
    size = 0
    header = bytearray()
    try:
        path.chmod(0o600)
        with os.fdopen(descriptor, "wb") as handle:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise AttachmentValidationError("invalid_download_chunk")
                size += len(chunk)
                if size > MAX_DOWNLOAD_BYTES:
                    raise AttachmentValidationError("file_too_large")
                if len(header) < 16:
                    header.extend(chunk[: 16 - len(header)])
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        for expected in (declared_size, remote_size):
            if expected is not None and expected != size:
                raise AttachmentValidationError("attachment_size_mismatch")
        media_type = _validated_media_type(kind, bytes(header), declared_mime_type)
        yield StagedAttachment(path, digest.hexdigest(), size, media_type)
    finally:
        path.unlink(missing_ok=True)


def _validated_media_type(
    kind: AttachmentKind, header: bytes, declared_mime_type: str
) -> str:
    actual: str | None = None
    if header.startswith(b"%PDF-"):
        actual = "application/pdf"
    elif header.startswith(b"\xff\xd8\xff"):
        actual = "image/jpeg"
    elif header.startswith(b"\x89PNG\r\n\x1a\n"):
        actual = "image/png"
    elif header.startswith(b"OggS"):
        actual = "audio/ogg"
    if actual is None:
        raise AttachmentValidationError("unsupported_attachment_signature")
    allowed = {
        "document": {"application/pdf", "image/jpeg", "image/png"},
        "photo": {"image/jpeg", "image/png"},
        "voice": {"audio/ogg"},
    }[kind]
    if actual not in allowed:
        raise AttachmentValidationError("attachment_kind_mismatch")
    declared = declared_mime_type.partition(";")[0].strip().casefold()
    aliases = {
        "audio/opus": "audio/ogg",
        "application/octet-stream": actual,
        "": actual,
    }
    normalized_declared = aliases.get(declared, declared)
    if normalized_declared != actual:
        raise AttachmentValidationError("attachment_mime_mismatch")
    return actual
