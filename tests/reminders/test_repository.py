from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from health_agent.models import Profile
from health_agent.reminders.models import (
    HealthReminder,
    HealthReminderEvent,
    ReminderStatus,
)
from health_agent.reminders.repository import (
    InvalidReminderTransition,
    ReminderNotFound,
    ReminderRepository,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)
DUE = datetime(2026, 9, 6, 7, 0, tzinfo=UTC)


def _propose(repository: ReminderRepository, profile_id: UUID = PROFILE_ID):
    return repository.propose(
        profile_id=profile_id,
        title="Repeat ferritin test",
        reason="Doctor requested a repeat after treatment",
        source_type="doctor_note",
        source_reference="document:abc",
        due_at=DUE,
        timezone_name="Europe/Moscow",
        now=NOW,
        public_code="safe-code-1" if profile_id == PROFILE_ID else "safe-code-2",
    )


def test_proposal_is_inactive_until_explicit_confirmation(session: Session) -> None:
    repository = ReminderRepository(session)
    proposed = _propose(repository)

    assert proposed.status is ReminderStatus.PENDING_CONFIRMATION
    assert proposed.confirmed_at is None
    assert repository.due_occurrences(DUE + timedelta(days=1)) == ()

    confirmed = repository.confirm(PROFILE_ID, proposed.public_code, now=NOW)
    repeated = repository.confirm(PROFILE_ID, proposed.public_code, now=NOW)

    assert confirmed.status is ReminderStatus.SCHEDULED
    assert confirmed.confirmed_at == NOW
    assert repeated == confirmed
    assert repository.due_occurrences(DUE - timedelta(seconds=1)) == ()
    assert repository.due_occurrences(DUE) == (confirmed,)
    events = session.scalars(
        select(HealthReminderEvent).order_by(HealthReminderEvent.occurred_at)
    ).all()
    assert [event.event_type for event in events] == ["proposed", "confirmed"]


def test_profile_scope_is_required_for_every_transition(session: Session) -> None:
    session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
    session.flush()
    repository = ReminderRepository(session)
    proposed = _propose(repository)

    with pytest.raises(ReminderNotFound):
        repository.confirm(OTHER_PROFILE_ID, proposed.public_code, now=NOW)
    with pytest.raises(ReminderNotFound):
        repository.get(OTHER_PROFILE_ID, proposed.public_code)
    assert (
        repository.get(PROFILE_ID, proposed.public_code).status
        is ReminderStatus.PENDING_CONFIRMATION
    )


def test_snooze_reschedule_complete_and_cancel_are_audited(session: Session) -> None:
    repository = ReminderRepository(session)
    proposed = _propose(repository)
    repository.confirm(PROFILE_ID, proposed.public_code, now=NOW)
    repository.mark_due_delivered(
        PROFILE_ID, proposed.id, delivery_revision=1, delivered_at=DUE
    )

    snoozed = repository.snooze(
        PROFILE_ID,
        proposed.public_code,
        duration=timedelta(hours=2),
        now=DUE,
    )
    assert snoozed.due_at == DUE + timedelta(hours=2)
    assert snoozed.delivery_revision == 2
    assert snoozed.delivered_at is None

    new_due = DUE + timedelta(days=2)
    moved = repository.reschedule(
        PROFILE_ID,
        proposed.public_code,
        due_at=new_due,
        timezone_name="UTC",
        now=DUE,
    )
    assert moved.due_at == new_due
    assert moved.timezone_name == "UTC"
    assert moved.delivery_revision == 3

    completed = repository.complete(PROFILE_ID, proposed.public_code, now=new_due)
    assert completed.status is ReminderStatus.COMPLETED
    assert (
        repository.complete(PROFILE_ID, proposed.public_code, now=new_due) == completed
    )
    with pytest.raises(InvalidReminderTransition):
        repository.cancel(PROFILE_ID, proposed.public_code, now=new_due)

    second = repository.propose(
        profile_id=PROFILE_ID,
        title="Blood pressure check",
        reason="Track a new symptom",
        source_type="user",
        source_reference="telegram",
        due_at=new_due,
        timezone_name="UTC",
        now=NOW,
        public_code="safe-code-3",
    )
    cancelled = repository.cancel(PROFILE_ID, second.public_code, now=NOW)
    assert cancelled.status is ReminderStatus.CANCELLED
    assert repository.cancel(PROFILE_ID, second.public_code, now=NOW) == cancelled

    event_types = session.scalars(
        select(HealthReminderEvent.event_type).order_by(HealthReminderEvent.id)
    ).all()
    assert event_types.count("completed") == 1
    assert event_types.count("cancelled") == 1


def test_delivery_ack_is_revision_fenced_and_idempotent(session: Session) -> None:
    repository = ReminderRepository(session)
    proposed = _propose(repository)
    assert repository.mark_proposal_notified(PROFILE_ID, proposed.id, notified_at=NOW)
    assert repository.mark_proposal_notified(PROFILE_ID, proposed.id, notified_at=NOW)
    repository.confirm(PROFILE_ID, proposed.public_code, now=NOW)
    assert repository.mark_due_delivered(
        PROFILE_ID, proposed.id, delivery_revision=1, delivered_at=DUE
    )
    assert repository.mark_due_delivered(
        PROFILE_ID, proposed.id, delivery_revision=1, delivered_at=DUE
    )

    repository.reschedule(
        PROFILE_ID,
        proposed.public_code,
        due_at=DUE + timedelta(days=1),
        timezone_name="Europe/Moscow",
        now=DUE,
    )
    assert not repository.mark_due_delivered(
        PROFILE_ID, proposed.id, delivery_revision=1, delivered_at=DUE
    )


def test_status_counts_are_profile_scoped_and_content_free(session: Session) -> None:
    session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
    session.flush()
    first = ReminderRepository(session)
    pending = _propose(first)
    _propose(first, OTHER_PROFILE_ID)
    first.confirm(PROFILE_ID, pending.public_code, now=NOW)

    status = first.status(PROFILE_ID, now=DUE)

    assert status.total == 1
    assert status.scheduled == 1
    assert status.due == 1
    assert status.pending_confirmation == 0


def test_database_forbids_scheduled_row_without_confirmation(session: Session) -> None:
    session.add(
        HealthReminder(
            id=uuid4(),
            profile_id=PROFILE_ID,
            public_code="constraint-1",
            title="Unsafe",
            reason="Must be rejected",
            source_type="test",
            source_reference="test",
            due_at=DUE,
            timezone_name="UTC",
            status=ReminderStatus.SCHEDULED,
            confirmed_at=None,
            delivery_revision=1,
            created_at=NOW,
            updated_at=NOW,
        )
    )
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_migration_tables_and_constraints_exist(clean_database) -> None:
    with clean_database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public'"
                )
            )
        }
        constraints = {
            row[0]
            for row in connection.execute(
                text(
                    "SELECT conname FROM pg_constraint WHERE conrelid = "
                    "'health_reminders'::regclass"
                )
            )
        }
    assert {"health_reminders", "health_reminder_events"} <= tables
    assert "ck_health_reminders_confirmation" in constraints
    assert "ck_health_reminders_terminal_timestamps" in constraints
