from uuid import uuid4

import pytest

from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.storage import PilotStore


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
