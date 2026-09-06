import json
import stat
from pathlib import Path
from uuid import uuid4

import pytest

from health_agent.google_calendar.models import CalendarProfile
from health_agent.google_calendar.stores import CalendarProfileStore, CalendarTokenStore


def test_private_store_roundtrip_and_subject_exclusion(tmp_path: Path):
    first, second = uuid4(), uuid4()
    profiles = CalendarProfileStore(tmp_path / "profiles")
    profile = CalendarProfile(first, calendar_id="team/a", enabled=True)
    profiles.save(profile)
    assert profiles.load(first) == profile
    assert stat.S_IMODE(profiles.path_for(first).stat().st_mode) == 0o600
    profiles.save(
        CalendarProfile(first, account_subject="bound", account_email="a@b.test")
    )
    with pytest.raises(ValueError, match="different account"):
        profiles.save(
            CalendarProfile(first, account_subject="changed", account_email="c@d.test")
        )
    tokens = CalendarTokenStore(tmp_path / "tokens")
    tokens.publish_verified(first, "subject", "a@b.test", {"token": "x"})
    with pytest.raises(ValueError, match="another profile"):
        tokens.publish_verified(second, "subject", "a@b.test", {"token": "y"})
    with pytest.raises(ValueError, match="different account"):
        tokens.publish_verified(first, "changed", "c@d.test", {"token": "y"})


def test_symlink_and_profile_mismatch_rejected(tmp_path: Path):
    profile = uuid4()
    store = CalendarProfileStore(tmp_path / "profiles")
    path = store.path_for(profile)
    outside = tmp_path / "outside"
    outside.write_text("{}")
    path.symlink_to(outside)
    with pytest.raises(RuntimeError, match="symlink"):
        store.load(profile)

    tokens = CalendarTokenStore(tmp_path / "tokens")
    token_path = tokens.path_for(profile)
    token_path.write_text(json.dumps({"profile_id": str(uuid4())}))
    token_path.chmod(0o600)
    with pytest.raises(ValueError, match="another profile"):
        tokens.load_verified(profile)
