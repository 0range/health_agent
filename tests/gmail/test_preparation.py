from __future__ import annotations

import base64
from pathlib import Path

import pytest

from health_agent.gmail.preparation import (
    AttachmentSizeMismatch,
    AttachmentTooLarge,
    AttachmentTypeMismatch,
    SafeAttachmentPreparer,
    iter_base64url_chunks,
)
from health_agent.gmail.types import AttachmentProvenance

PROFILE = "11111111-1111-1111-1111-111111111111"


def provenance(mime_type: str = "application/pdf") -> AttachmentProvenance:
    return AttachmentProvenance(
        PROFILE,
        "personal",
        "alice@example.com",
        "m1",
        "t1",
        "10",
        1000,
        "1",
        "a1",
        "document.pdf",
        mime_type,
        "suspected_medical",
        "https://mail.google.com/mail/#all/m1",
    )


def encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def test_prepares_private_validated_pdf_and_removes_temporary_file(
    tmp_path: Path,
) -> None:
    content = b"%PDF-1.4\ncontent"
    preparer = SafeAttachmentPreparer(tmp_path, 1024)
    with preparer.prepare(provenance(), encode(content), len(content)) as prepared:
        path = prepared.path
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o077 == 0
        assert prepared.detected_mime_type == "application/pdf"
    assert not path.exists()


def test_size_and_magic_fail_before_downstream_import(tmp_path: Path) -> None:
    preparer = SafeAttachmentPreparer(tmp_path, 8)
    with pytest.raises(AttachmentTooLarge):
        preparer.validate_before_download(9)
    with (
        pytest.raises(AttachmentSizeMismatch),
        preparer.prepare(provenance(), encode(b"%PDF-"), 4),
    ):
        pass
    with (
        pytest.raises(AttachmentTypeMismatch),
        preparer.prepare(provenance(), encode(b"not pdf"), 7),
    ):
        pass


def test_decoder_is_incremental_and_strict() -> None:
    content = bytes(range(256)) * 100
    assert b"".join(iter_base64url_chunks(encode(content), 17)) == content
    with pytest.raises(ValueError):
        b"".join(iter_base64url_chunks("%%%", 4))
