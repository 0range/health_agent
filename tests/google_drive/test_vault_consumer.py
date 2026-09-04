from __future__ import annotations

import hashlib
import stat
from pathlib import Path

import pytest

from health_agent.google_drive.types import DriveItem, DriveProvenance
from health_agent.google_drive.vault_consumer import FileVaultDriveConsumer

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


def provenance(profile_id: str) -> DriveProvenance:
    return DriveProvenance(
        profile_id=profile_id,
        root_folder_id="root-folder",
        folder_path=("Medical",),
        item=DriveItem(
            file_id="file-1",
            name="lab.pdf",
            mime_type="application/pdf",
            parent_ids=("root-folder",),
        ),
        output_media_type="application/pdf",
        exported_from_google_native=False,
    )


def test_streams_chunks_to_profile_isolated_vault(tmp_path: Path) -> None:
    consumer = FileVaultDriveConsumer(ALICE, tmp_path / "vault", tmp_path / "tmp")
    receipt = consumer.consume(provenance(ALICE), iter((b"medical", b"-pdf")))

    assert receipt.sha256 == hashlib.sha256(b"medical-pdf").hexdigest()
    assert receipt.size_bytes == 11
    stored = Path(receipt.storage_reference)
    assert stored.read_bytes() == b"medical-pdf"
    assert ALICE in stored.parts
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert list((tmp_path / "tmp" / ALICE).iterdir()) == []


def test_rejects_content_for_another_profile(tmp_path: Path) -> None:
    consumer = FileVaultDriveConsumer(ALICE, tmp_path / "vault", tmp_path / "tmp")
    with pytest.raises(ValueError, match="different profile"):
        consumer.consume(provenance(BOB), iter((b"bytes",)))
