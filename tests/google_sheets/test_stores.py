from __future__ import annotations

import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from health_agent.google_sheets.config import SHEETS_SCOPES, SheetsProfile
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsTokenStore,
)
from health_agent.google_sheets.types import SheetsAccountIdentity


def _token() -> str:
    return json.dumps({"token": "private", "scopes": sorted(SHEETS_SCOPES)})


def test_profile_store_is_private_and_profile_scoped(tmp_path: Path) -> None:
    profile_id = str(uuid4())
    profile = SheetsProfile.create(profile_id)
    store = LocalSheetsProfileStore(tmp_path)
    store.save(profile)
    assert store.load(profile_id) == profile
    assert stat.S_IMODE(store.path_for(profile_id).stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path_for(profile_id).parent.stat().st_mode) == 0o700


def test_token_store_rejects_cross_profile_account_reuse(tmp_path: Path) -> None:
    first, second = str(uuid4()), str(uuid4())
    store = LocalSheetsTokenStore(tmp_path)
    identity = SheetsAccountIdentity("permission-1", "me@example.com")
    store.publish_verified(first, identity, _token())
    with pytest.raises(ValueError, match="another health profile"):
        store.publish_verified(second, identity, _token())


def test_stores_refuse_symlinked_files(tmp_path: Path) -> None:
    profile_id = str(uuid4())
    store = LocalSheetsProfileStore(tmp_path / "sheets")
    path = store.path_for(profile_id)
    target = tmp_path / "outside"
    target.write_text("{}")
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlinked"):
        store.load(profile_id)
