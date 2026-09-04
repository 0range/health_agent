from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from health_agent.gmail.types import AttachmentProvenance
from health_agent.gmail.vault_importer import VaultAttachmentImporter

PROFILE_A = "11111111-1111-1111-1111-111111111111"
PROFILE_B = "22222222-2222-2222-2222-222222222222"


def provenance(profile_id: str = PROFILE_A, account_id: str = "personal") -> AttachmentProvenance:
    return AttachmentProvenance(
        profile_id,
        account_id,
        "me@example.com",
        "m1",
        "t1",
        "10",
        1000,
        "1",
        "a1",
        "labs.pdf",
        "application/pdf",
        "suspected_medical",
        "https://mail.google.com/mail/u/0/#all/m1",
    )


def test_streams_to_profile_account_isolated_vault(tmp_path: Path) -> None:
    importer = VaultAttachmentImporter(
        PROFILE_A, "personal", tmp_path / "vault", tmp_path / "tmp"
    )
    receipt = importer.import_attachment(provenance(), iter((b"medical", b"-pdf")))
    stored = Path(receipt.storage_reference)
    assert receipt.sha256 == hashlib.sha256(b"medical-pdf").hexdigest()
    assert stored.read_bytes() == b"medical-pdf"
    assert PROFILE_A in stored.parts
    assert "personal" in stored.parts
    assert list((tmp_path / "tmp" / PROFILE_A / "gmail" / "personal").iterdir()) == []


def test_rejects_cross_profile_or_account_content(tmp_path: Path) -> None:
    importer = VaultAttachmentImporter(
        PROFILE_A, "personal", tmp_path / "vault", tmp_path / "tmp"
    )
    with pytest.raises(ValueError, match="another profile"):
        importer.import_attachment(provenance(PROFILE_B), iter((b"x",)))
    with pytest.raises(ValueError, match="another account"):
        importer.import_attachment(provenance(PROFILE_A, "work"), iter((b"x",)))
