from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from health_agent.google_calendar.models import CalendarEvent, CalendarProfile
from health_agent.google_calendar.service import CalendarService, event_id
from health_agent.google_calendar.stores import CalendarProfileStore, CalendarTokenStore


class FakeGateway:
    def __init__(self):
        self.events = {}
        self.insert_count = 0
        self.patch_count = 0

    def get(self, calendar_id, event_id):
        return self.events.get(event_id)

    def insert(self, calendar_id, body):
        self.insert_count += 1
        stored = {**body, "etag": '"1"', "htmlLink": "https://calendar.test/event"}
        self.events[body["id"]] = stored
        return stored

    def patch(self, calendar_id, event_id, body, etag):
        self.patch_count += 1
        stored = {**self.events[event_id], **body, "etag": '"2"'}
        self.events[event_id] = stored
        return stored


def event(profile_id=None, visit_id=None, **changes):
    values = {
        "profile_id": profile_id or uuid4(),
        "visit_id": visit_id or uuid4(),
        "title": "Visit",
        "starts_at": datetime(2026, 9, 8, 7, tzinfo=UTC),
        "ends_at": datetime(2026, 9, 8, 8, tzinfo=UTC),
        "timezone_name": "Europe/Moscow",
        "questions": ("Could <script>alert(1)</script> be relevant?",),
    }
    values.update(changes)
    return CalendarEvent(**values)


def configured(tmp_path: Path, profile_id):
    profiles = CalendarProfileStore(tmp_path / "profiles")
    tokens = CalendarTokenStore(tmp_path / "tokens")
    profiles.save(
        CalendarProfile(
            profile_id, enabled=True, account_subject="sub", account_email="a@b.test"
        )
    )
    tokens.publish_verified(profile_id, "sub", "a@b.test", {"token": "secret"})
    return profiles, tokens


def test_deterministic_ids_and_validation():
    owner, other, visit = uuid4(), uuid4(), uuid4()
    assert event_id(owner, visit) == event_id(owner, visit)
    assert event_id(owner, visit) != event_id(other, visit)
    assert len(event_id(owner, visit)) == 64
    with pytest.raises(ValueError):
        event(title="")
    with pytest.raises(ValueError):
        event(ends_at=datetime(2026, 9, 8, 6, tzinfo=UTC))
    with pytest.raises(ValueError):
        event(questions=tuple("q" for _ in range(21)))


def test_create_update_repeat_cancel_and_escaped_content(tmp_path: Path):
    item = event()
    profiles, tokens = configured(tmp_path, item.profile_id)
    gateway = FakeGateway()
    service = CalendarService(profiles, tokens, None, lambda _: gateway)
    assert service.sync(item).status == "created"
    assert service.sync(item).status == "unchanged"
    assert gateway.insert_count == 1
    assert (
        "&lt;script&gt;"
        in gateway.events[event_id(item.profile_id, item.visit_id)]["description"]
    )
    changed = event(item.profile_id, item.visit_id, questions=("Updated",))
    assert service.sync(changed).status == "updated"
    assert (
        service.sync(event(item.profile_id, item.visit_id, cancelled=True)).status
        == "cancelled"
    )


def test_foreign_remote_event_fails_closed(tmp_path: Path):
    item = event()
    profiles, tokens = configured(tmp_path, item.profile_id)
    gateway = FakeGateway()
    gateway.events[event_id(item.profile_id, item.visit_id)] = {
        "id": event_id(item.profile_id, item.visit_id),
        "etag": '"x"',
        "extendedProperties": {
            "private": {"profile_id": str(uuid4()), "visit_id": str(item.visit_id)}
        },
    }
    result = CalendarService(profiles, tokens, None, lambda _: gateway).sync(item)
    assert result.safe_error == "remote_ownership_mismatch"
    assert gateway.patch_count == 0


def test_cancelled_absent_event_is_not_created(tmp_path: Path):
    item = event(cancelled=True)
    profiles, tokens = configured(tmp_path, item.profile_id)
    gateway = FakeGateway()
    assert (
        CalendarService(profiles, tokens, None, lambda _: gateway).sync(item).status
        == "unchanged"
    )
    assert gateway.insert_count == 0
