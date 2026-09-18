from datetime import date
from uuid import uuid4

from test_training import MemoryStore

from health_agent.pilot.sleep_context import night_contexts, sleep_comparisons


def test_context_is_scoped_dated_and_keeps_plans_separate_from_reports():
    store, profile, other = MemoryStore(), uuid4(), uuid4()
    payload = {"wake_date": "2027-01-03", "night_start_date": "2027-01-02",
               "location": "away", "evidence": "planned", "source": "user",
               "timezone": "Europe/Moscow", "location_label": "Conference",
               "source_text": "private original"}
    store.put(profile, "shared", "sleep_context", "own", payload)
    store.put(other, "shared", "sleep_context", "foreign", {**payload, "location_label": "secret"})
    store.put(profile, "shared", "sleep_context", "bad", {**payload, "wake_date": "broken"})
    store.put(profile, "shared", "sleep_context", "wrong-zone", {**payload, "timezone": "UTC"})
    result = night_contexts(store, profile, date(2027, 1, 3), date(2027, 1, 3))
    assert len(result) == 1 and result[0]["evidence"] == "planned"
    assert result[0]["room_comparison"] == "exclude_away"
    assert "private original" not in str(result) and "secret" not in str(result)
    assert night_contexts(store, profile, date(2027, 1, 4), date(2027, 1, 5)) == []


def test_sleep_exclusion_uses_moscow_wake_date_without_assuming_home_or_matching_naps():
    notes = [{"wake_date": "2027-01-03", "location": "away", "evidence": "planned",
              "room_comparison": "exclude_away"}]
    sleeps = [
        {"sleep_id": "night", "end_at": "2027-01-02T22:30:00+00:00", "nap": False},
        {"sleep_id": "unknown", "end_at": "2027-01-03T22:30:00+00:00", "nap": False},
        {"sleep_id": "nap", "end_at": "2027-01-03T12:30:00+00:00", "nap": True},
    ]
    result = sleep_comparisons(sleeps, notes)
    assert result[0]["wake_date"] == "2027-01-03"
    assert result[0]["room_comparison"] == "exclude_away"
    assert result[0]["evidence"] == "planned"
    assert result[1]["room_comparison"] == result[2]["room_comparison"] == "unknown"
