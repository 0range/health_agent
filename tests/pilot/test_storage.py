from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.storage import PilotRecord, PilotStore


def test_exact_source_lookup_outlives_history_caps_and_is_scoped(clean_database):
    store = PilotStore(clean_database)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    original = store.put(DEFAULT_PROFILE_ID, "food", "comment", "original", {"meal_id": "first"}, at=now)
    with session_scope(clean_database) as session:
        session.add_all([
            PilotRecord(profile_id=DEFAULT_PROFILE_ID, domain="food", kind="comment",
                        source_key=f"newer{index}", payload={}, at=now + timedelta(seconds=index + 1))
            for index in range(1001)
        ])
    assert original.id not in {r.id for r in store.list(DEFAULT_PROFILE_ID, "food", "comment", limit=1000)}
    assert store.by_source(DEFAULT_PROFILE_ID, "food", "comment", "original") == original
    assert store.by_source(uuid4(), "food", "comment", "original") is None
    assert store.by_source(DEFAULT_PROFILE_ID, "training", "comment", "original") is None
    assert store.by_source(DEFAULT_PROFILE_ID, "food", "photo", "original") is None
    assert store.by_source(DEFAULT_PROFILE_ID, "food", "comment", "missing") is None


def test_records_persist_replay_and_isolate(clean_database):
    store = PilotStore(clean_database)
    first = store.put(DEFAULT_PROFILE_ID, "sleep", "diary", "u1", {"text": "rested"})
    assert (
        store.put(DEFAULT_PROFILE_ID, "sleep", "diary", "u1", {"text": "rested"}).id
        == first.id
    )
    other = PilotStore(clean_database)
    assert other.list(DEFAULT_PROFILE_ID, "sleep")[0].payload == {"text": "rested"}
    assert other.get(uuid4(), first.id) is None
    with pytest.raises(ValueError):
        other.patch(uuid4(), first.id, {"text": "not mine"})
    other.patch(DEFAULT_PROFILE_ID, first.id, {"text": "corrected"})
    assert store.get(DEFAULT_PROFILE_ID, first.id).payload["text"] == "corrected"
    assert not store.list(DEFAULT_PROFILE_ID, "food")


def test_invalid_record_cannot_be_saved(clean_database):
    store = PilotStore(clean_database)
    with pytest.raises(ValueError):
        store.put(DEFAULT_PROFILE_ID, "sleep", "diary", "", {})
