from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select

from health_agent.db import session_scope
from health_agent.google_calendar.models import CalendarProfile, CalendarResult
from health_agent.google_calendar.publication import (
    CalendarPublicationService,
    VisitCalendarPublication,
    safe_calendar_link,
)
from health_agent.google_calendar.stores import CalendarProfileStore
from health_agent.visits.repository import VisitNotFound, VisitRepository

OWNER = UUID(int=1)


def create_visit(engine, owner=OWNER):
    with session_scope(engine) as session:
        return VisitRepository(session).create(
            owner,
            title="Synthetic visit",
            starts_at=datetime(2026, 10, 1, tzinfo=UTC),
            ends_at=datetime(2026, 10, 1, 1, tzinfo=UTC),
            timezone_name="Europe/Moscow",
            creation_key=str(uuid4()),
        )


class FakeCalendar:
    def __init__(self, root):
        self.profiles = CalendarProfileStore(root)
        self.profiles.save(
            CalendarProfile(
                OWNER,
                enabled=True,
                account_subject="owner",
                account_email="owner@test.invalid",
            )
        )
        self.calls = []
        self.callback = None
        self.oauth = SimpleNamespace(local_status=lambda _: "ready")

    def sync(self, event):
        self.calls.append(event)
        if self.callback:
            return self.callback(event)
        return CalendarResult(
            "stable", "created", "https://www.google.com/calendar/event?eid=synthetic"
        )


def publication(engine, tmp_path):
    calendar = FakeCalendar(tmp_path / "config")
    return CalendarPublicationService(engine, calendar, tmp_path / "locks"), calendar


def test_explicit_opt_in_idempotent_and_profile_bound(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    assert service.sync_visit(OWNER, visit.public_code).status == "unchanged"
    assert fake.calls == []
    assert service.publish(OWNER, visit.public_code).status == "published"
    assert service.publish(OWNER, visit.public_code).status == "unchanged"
    assert [event.visit_id for event in fake.calls] == [visit.id]
    with pytest.raises(VisitNotFound):
        service.publish(uuid4(), visit.public_code)
    with session_scope(clean_database) as session:
        assert (
            session.scalar(select(func.count()).select_from(VisitCalendarPublication))
            == 1
        )


def test_committed_questions_only_timeout_then_retry(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    with session_scope(clean_database) as session:
        repo = VisitRepository(session)
        repo.add_note(
            OWNER, visit.public_code, kind="question", text="A question", action_key="q"
        )
        repo.add_note(
            OWNER,
            visit.public_code,
            kind="answer",
            text="PRIVATE ANSWER",
            action_key="a",
        )

    def callback(event):
        with session_scope(clean_database) as session:
            assert len(VisitRepository(session).notes(OWNER, visit.public_code)) == 2
        assert event.questions == ("A question",)
        raise TimeoutError("sensitive detail")

    fake.callback = callback
    assert service.publish(OWNER, visit.public_code).status == "queued"
    assert service.snapshot(OWNER, visit.public_code).status == "queued"
    fake.callback = None
    assert service.sync_profile(OWNER)[0].status == "published"


def test_edited_during_sync_records_exact_sent_fingerprint(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)

    def callback(event):
        with session_scope(clean_database) as session:
            VisitRepository(session).add_note(
                OWNER,
                visit.public_code,
                kind="question",
                text="Later",
                action_key="later",
            )
        return CalendarResult("stable", "created")

    fake.callback = callback
    assert service.publish(OWNER, visit.public_code).status == "queued"
    fake.callback = None
    assert service.sync_visit(OWNER, visit.public_code).status == "published"
    assert fake.calls[-1].questions == ("Later",)
    assert service.sync_visit(OWNER, visit.public_code).status == "unchanged"


def test_target_change_is_not_silently_republished(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    service.publish(OWNER, visit.public_code)
    fake.profiles.save(
        CalendarProfile(
            OWNER,
            calendar_id="other",
            enabled=True,
            account_subject="owner",
            account_email="owner@test.invalid",
        )
    )
    result = service.sync_visit(OWNER, visit.public_code)
    assert result.status == "queued" and result.safe_error == "calendar_target_mismatch"
    assert len(fake.calls) == 1


def test_question_limits_and_safe_link(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    with session_scope(clean_database) as session:
        repo = VisitRepository(session)
        for index in range(22):
            repo.add_note(
                OWNER,
                visit.public_code,
                kind="question",
                text=str(index) + "x" * 1100,
                action_key=f"q{index}",
            )
    fake.callback = lambda _: CalendarResult(
        "stable", "created", "https://evil.test/?secret=1"
    )
    result = service.publish(OWNER, visit.public_code)
    assert result.html_link is None
    assert len(fake.calls[0].questions) == 20
    assert all(len(q) <= 1000 for q in fake.calls[0].questions)
    assert "20" in fake.calls[0].questions[-1]


def test_cancelled_opted_in_visit_sends_cancellation(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    service.publish(OWNER, visit.public_code)
    with session_scope(clean_database) as session:
        VisitRepository(session).cancel(OWNER, visit.public_code)
    service.sync_visit(OWNER, visit.public_code)
    assert fake.calls[-1].cancelled is True


def test_same_visit_delivery_and_target_configuration_are_locked(
    clean_database, tmp_path
):
    from health_agent.automation.storage import GlobalRunLock

    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)

    def callback(event):
        assert (
            service.sync_visit(OWNER, visit.public_code).safe_error
            == "publication_busy"
        )
        target_lock = GlobalRunLock(fake.profiles.root / "publish.lock")
        assert not target_lock.acquire()
        return CalendarResult("stable", "created")

    fake.callback = callback
    assert service.publish(OWNER, visit.public_code).status == "published"
    assert len(fake.calls) == 1


def test_no_network_while_transaction_is_open(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)

    def callback(event):
        assert clean_database.pool.checkedout() == 0
        return CalendarResult("stable", "created")

    fake.callback = callback
    assert service.publish(OWNER, visit.public_code).status == "published"


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "http://www.google.com/calendar/event",
        "https://google.com.evil.test/calendar/event",
        "https://user@www.google.com/calendar/event",
        "https://www.google.com:444/calendar/event",
        "https://www.google.com/calendar/event\n",
        "//www.google.com/calendar/event",
    ],
)
def test_untrusted_event_urls_are_discarded(url):
    assert safe_calendar_link(url) is None


def test_unknown_adapter_error_is_redacted(clean_database, tmp_path):
    visit = create_visit(clean_database)
    service, fake = publication(clean_database, tmp_path)
    fake.callback = lambda _: CalendarResult(
        "stable", "deferred", safe_error="private raw error"
    )
    result = service.publish(OWNER, visit.public_code)
    assert result.safe_error == "calendar_sync_failed"
    assert "private" not in str(service.snapshot(OWNER, visit.public_code))
