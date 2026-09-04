from __future__ import annotations

import json
import stat
from dataclasses import asdict
from pathlib import Path

import pytest

from health_agent.google_drive.config import DriveProfile
from health_agent.google_drive.stores import (
    LocalProfileStore,
    LocalSyncStateStore,
    LocalTokenStore,
)
from health_agent.google_drive.types import SeenItem


def _seen(profile_id: str, file_id: str = "drive-file-1") -> SeenItem:
    return SeenItem(
        profile_id=profile_id,
        file_id=file_id,
        revision="7|||2026-09-04T10:00:00Z",
        root_folder_id="root-folder-123",
        ancestor_folder_ids=("root-folder-123",),
        folder_path=("Medical",),
        source_url=f"https://drive.google.com/file/d/{file_id}/view",
        source_name="labs.pdf",
        source_mime_type="application/pdf",
        output_media_type="application/pdf",
        drive_id=None,
        drive_version="7",
        created_time="2026-09-01T10:00:00Z",
        modified_time="2026-09-04T10:00:00Z",
        head_revision_id="head-7",
        drive_md5_checksum="md5",
        sha256="a" * 64,
        size_bytes=42,
        storage_reference="vault/ref",
        status="imported",
    )


def test_profile_token_and_state_are_private_and_profile_isolated(tmp_path: Path) -> None:
    profiles = LocalProfileStore(tmp_path)
    tokens = LocalTokenStore(tmp_path)
    state = LocalSyncStateStore(tmp_path)
    root = "root-folder-123"
    profiles.save(DriveProfile.create("alice", [root]))
    profiles.save(DriveProfile.create("bob", [root]))
    token_path = tokens.save("alice", json.dumps({"token": "secret"}))
    state.record_seen(_seen("alice"))
    state.set_cursor("alice", "cursor-a")

    assert profiles.load("alice").profile_id == "alice"
    assert state.get_cursor("alice") == "cursor-a"
    assert state.get_cursor("bob") is None
    assert state.get_seen("bob", "drive-file-1") is None
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "alice" / "profile.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "alice" / "sync-state.json").stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "alice").stat().st_mode) == 0o700


def test_store_rejects_cross_profile_payload(tmp_path: Path) -> None:
    state = LocalSyncStateStore(tmp_path)
    path = tmp_path / "bob" / "sync-state.json"
    path.parent.mkdir(parents=True)
    payload = _seen("alice")
    path.write_text(
        json.dumps({"cursor": None, "items": {payload.file_id: asdict(payload)}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different profile"):
        state.get_seen("bob", payload.file_id)


def test_rewrites_overly_permissive_state_mode(tmp_path: Path) -> None:
    store = LocalProfileStore(tmp_path)
    store.save(DriveProfile.create("alice", ["root-folder-123"]))
    path = tmp_path / "alice" / "profile.json"
    path.chmod(0o644)
    store.load("alice")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
