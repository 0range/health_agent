"""Bounded base64url decoding and file-type validation before import effects."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from health_agent.gmail.types import AttachmentProvenance, PreparedAttachment

DEFAULT_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


class AttachmentPreparationError(ValueError):
    """Safe base class for a rejected Gmail attachment."""


class InvalidAttachmentEncoding(AttachmentPreparationError):
    pass


class AttachmentTooLarge(AttachmentPreparationError):
    pass


class AttachmentSizeMismatch(AttachmentPreparationError):
    pass


class AttachmentTypeMismatch(AttachmentPreparationError):
    pass


class SafeAttachmentPreparer:
    def __init__(self, temporary_root: Path, max_bytes: int) -> None:
        if max_bytes < 1:
            raise ValueError("attachment size limit must be positive")
        self.temporary_root = Path(temporary_root)
        self.max_bytes = max_bytes

    def validate_before_download(self, declared_size: int | None) -> None:
        if declared_size is not None and declared_size > self.max_bytes:
            raise AttachmentTooLarge("declared Gmail attachment exceeds size limit")

    @contextmanager
    def prepare(
        self,
        provenance: AttachmentProvenance,
        encoded: str,
        declared_size: int | None,
    ) -> Iterator[PreparedAttachment]:
        self.validate_before_download(declared_size)
        # Four base64 characters encode at most three bytes. Allow padding only.
        if len(encoded) > ((self.max_bytes + 2) // 3) * 4 + 4:
            raise AttachmentTooLarge("encoded Gmail attachment exceeds size limit")
        directory = (
            self.temporary_root
            / provenance.profile_id
            / "gmail"
            / provenance.account_id
        )
        _private_directory(directory)
        descriptor, name = tempfile.mkstemp(
            dir=directory, prefix="prepare-", suffix=".partial"
        )
        path = Path(name)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(descriptor, "wb") as handle:
                for chunk in iter_base64url_chunks(encoded):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise AttachmentTooLarge(
                            "decoded Gmail attachment exceeds size limit"
                        )
                    handle.write(chunk)
                    digest.update(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            path.chmod(0o600)
            if declared_size is not None and size != declared_size:
                raise AttachmentSizeMismatch(
                    "Gmail attachment size does not match metadata"
                )
            detected = detect_mime_type(path)
            if not _mime_matches(provenance.source_mime_type, detected):
                raise AttachmentTypeMismatch(
                    "Gmail attachment signature does not match MIME"
                )
            assert detected is not None
            yield PreparedAttachment(path, digest.hexdigest(), size, detected)
        finally:
            path.unlink(missing_ok=True)


def iter_base64url_chunks(
    data: str, encoded_chunk_size: int = 65536
) -> Iterator[bytes]:
    """Incrementally decode Gmail's materialized, unpadded base64url value."""
    if encoded_chunk_size < 4:
        raise ValueError("encoded chunk size must be at least four")
    try:
        data.encode("ascii")
    except UnicodeEncodeError as error:
        raise InvalidAttachmentEncoding(
            "attachment data is not ASCII base64url"
        ) from error
    pending = ""
    for offset in range(0, len(data), encoded_chunk_size):
        pending += data[offset : offset + encoded_chunk_size]
        decodable = len(pending) - (len(pending) % 4)
        if decodable:
            block, pending = pending[:decodable], pending[decodable:]
            try:
                yield base64.b64decode(block, altchars=b"-_", validate=True)
            except (binascii.Error, ValueError) as error:
                raise InvalidAttachmentEncoding(
                    "invalid Gmail base64url data"
                ) from error
    if pending:
        try:
            yield base64.b64decode(
                pending + "=" * (-len(pending) % 4), altchars=b"-_", validate=True
            )
        except (binascii.Error, ValueError) as error:
            raise InvalidAttachmentEncoding("invalid Gmail base64url data") from error


def detect_mime_type(path: Path) -> str | None:
    with path.open("rb") as handle:
        head = handle.read(32)
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in {b"heic", b"heix", b"hevc", b"hevx"}:
            return "image/heic"
        if brand in {b"mif1", b"msf1"}:
            return "image/heif"
    return None


def _mime_matches(declared: str, detected: str | None) -> bool:
    if declared in {"image/heic", "image/heif"}:
        return detected in {"image/heic", "image/heif"}
    return declared == detected


def _private_directory(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    if any(component.is_symlink() for component in (absolute, *absolute.parents)):
        raise RuntimeError("refusing symlinked Gmail temporary directory")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError("refusing non-directory Gmail temporary path")
    path.chmod(0o700)
