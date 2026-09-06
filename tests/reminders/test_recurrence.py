from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from health_agent.db import session_scope
from health_agent.models import Base, Profile
from health_agent.reminders.models import HealthReminder, ReminderStatus
from health_agent.reminders.repository import ReminderNotFound, ReminderRepository
from health_agent.reminders.time import next_recurrence_due

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


def _repeating(repository: ReminderRepository, *, due: datetime, unit: str, every: int):
    reminder = repository.propose(
        profile_id=PROFILE_ID,
        title="Annual checkup",
        reason="User requested a recurring reminder",
        source_type="user",
        source_reference="telegram",
        due_at=due,
        timezone_name="Europe/Moscow",
        repeat_unit=unit,
        repeat_every=every,
        now=datetime(2026, 9, 1, tzinfo=UTC),
        public_code="annual-checkup",
    )
    return repository.confirm(PROFILE_ID, reminder.public_code, now=due)


def test_completion_creates_one_confirmed_successor_and_replay_is_idempotent(
    clean_database: Engine,
) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        parent = _repeating(repository, due=due, unit="months", every=12)
        completed = repository.complete(
            PROFILE_ID, parent.public_code, now=due, action_key="done-1"
        )
        child = repository.successor(PROFILE_ID, parent.public_code)

        assert completed.status is ReminderStatus.COMPLETED
        assert child is not None
        assert child.status is ReminderStatus.SCHEDULED
        assert child.confirmed_at == due
        assert child.due_at == datetime(2027, 9, 6, 7, tzinfo=UTC)
        assert child.recurrence_parent_id == parent.id
        assert child.repeat_unit == "months"
        assert child.repeat_every == 12
        assert child.delivered_at is None

        replayed = repository.complete(
            PROFILE_ID, parent.public_code, now=due, action_key="done-2"
        )
        assert replayed == completed
        assert repository.successor(PROFILE_ID, parent.public_code).id == child.id  # type: ignore[union-attr]
        assert session.scalar(select(func.count()).select_from(HealthReminder)) == 2


def test_late_completion_uses_completion_wall_clock_and_cancel_stops_chain(
    clean_database: Engine,
) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    completed_at = datetime(2026, 9, 10, 8, 30, tzinfo=UTC)
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        parent = _repeating(repository, due=due, unit="days", every=7)
        repository.complete(PROFILE_ID, parent.public_code, now=completed_at)
        child = repository.successor(PROFILE_ID, parent.public_code)
        assert child is not None
        assert child.due_at == datetime(2026, 9, 17, 8, 30, tzinfo=UTC)

        repository.cancel(PROFILE_ID, child.public_code, now=completed_at)
        assert repository.successor(PROFILE_ID, child.public_code) is None


def test_one_shot_completion_remains_unchanged(clean_database: Engine) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        reminder = repository.propose(
            profile_id=PROFILE_ID,
            title="One shot",
            reason="Only once",
            source_type="user",
            source_reference="telegram",
            due_at=due,
            timezone_name="UTC",
            now=due,
            public_code="one-shot-code",
        )
        repository.confirm(PROFILE_ID, reminder.public_code, now=due)
        repository.complete(PROFILE_ID, reminder.public_code, now=due)
        assert repository.successor(PROFILE_ID, reminder.public_code) is None


@pytest.mark.parametrize(
    ("unit", "every"),
    [
        (None, 1),
        ("days", None),
        ("weeks", 1),
        ("days", 0),
        ("days", 3651),
        ("months", 121),
    ],
)
def test_invalid_recurrence_rejects_before_write(
    clean_database: Engine, unit: str | None, every: int | None
) -> None:
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        with pytest.raises(ValueError, match="invalid_recurrence"):
            repository.propose(
                profile_id=PROFILE_ID,
                title="Invalid",
                reason="Invalid",
                source_type="user",
                source_reference="telegram",
                due_at=datetime(2026, 9, 6, tzinfo=UTC),
                timezone_name="UTC",
                repeat_unit=unit,
                repeat_every=every,
                public_code="invalid-repeat",
            )
        assert session.scalar(select(func.count()).select_from(HealthReminder)) == 0


def test_successor_is_profile_scoped(clean_database: Engine) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    with session_scope(clean_database) as session:
        session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
        repository = ReminderRepository(session)
        parent = _repeating(repository, due=due, unit="days", every=1)
        repository.complete(PROFILE_ID, parent.public_code, now=due)
        with pytest.raises(ReminderNotFound):
            repository.successor(OTHER_PROFILE_ID, parent.public_code)


def test_foreign_profile_cannot_mutate_or_replay_recurrence_chain(
    clean_database: Engine,
) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    with session_scope(clean_database) as session:
        session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
        repository = ReminderRepository(session)
        parent = _repeating(repository, due=due, unit="days", every=1)

        for transition in (repository.cancel, repository.complete):
            with pytest.raises(ReminderNotFound):
                transition(OTHER_PROFILE_ID, parent.public_code, now=due)
        assert (
            repository.get(PROFILE_ID, parent.public_code).status
            is ReminderStatus.SCHEDULED
        )
        assert repository.successor(PROFILE_ID, parent.public_code) is None

        repository.complete(
            PROFILE_ID, parent.public_code, now=due, action_key="rightful-done"
        )
        child = repository.successor(PROFILE_ID, parent.public_code)
        assert child is not None
        for code, action_key in (
            (parent.public_code, "rightful-done"),
            (parent.public_code, "foreign-replay"),
            (child.public_code, "foreign-child"),
        ):
            with pytest.raises(ReminderNotFound):
                repository.complete(
                    OTHER_PROFILE_ID, code, now=due, action_key=action_key
                )
        with pytest.raises(ReminderNotFound):
            repository.cancel(OTHER_PROFILE_ID, child.public_code, now=due)
        assert (
            repository.get(PROFILE_ID, parent.public_code).status
            is ReminderStatus.COMPLETED
        )
        assert (
            repository.get(PROFILE_ID, child.public_code).status
            is ReminderStatus.SCHEDULED
        )


def test_calendar_month_clamp_dst_gap_and_fold() -> None:
    assert next_recurrence_due(
        datetime(2028, 2, 29, 9, tzinfo=UTC),
        datetime(2028, 2, 29, 9, tzinfo=UTC),
        "UTC",
        "months",
        12,
    ) == datetime(2029, 2, 28, 9, tzinfo=UTC)
    assert next_recurrence_due(
        datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
        datetime(2026, 3, 28, 1, 30, tzinfo=UTC),
        "Europe/Berlin",
        "days",
        1,
    ) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert next_recurrence_due(
        datetime(2026, 10, 24, 0, 30, tzinfo=UTC),
        datetime(2026, 10, 24, 0, 30, tzinfo=UTC),
        "Europe/Berlin",
        "days",
        1,
    ) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="invalid_recurrence_date"):
        next_recurrence_due(
            datetime.max.replace(tzinfo=UTC),
            datetime.max.replace(tzinfo=UTC),
            "UTC",
            "days",
            1,
        )


def test_migrated_recurrence_schema_matches_models(clean_database: Engine) -> None:
    with clean_database.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
    assert differences == []


@pytest.mark.parametrize(
    ("repeat_unit", "repeat_every", "public_code"),
    [(None, 1, "partial-null-1"), ("days", None, "partial-null-2")],
)
def test_database_rejects_partial_null_recurrence_pairs(
    clean_database: Engine,
    repeat_unit: str | None,
    repeat_every: int | None,
    public_code: str,
) -> None:
    with pytest.raises(IntegrityError), session_scope(clean_database) as session:
        session.add(
            HealthReminder(
                profile_id=PROFILE_ID,
                public_code=public_code,
                title="Direct write",
                reason="Constraint regression",
                source_type="test",
                source_reference="test",
                due_at=datetime(2026, 9, 6, tzinfo=UTC),
                timezone_name="UTC",
                status=ReminderStatus.PENDING_CONFIRMATION.value,
                delivery_revision=1,
                repeat_unit=repeat_unit,
                repeat_every=repeat_every,
            )
        )
        session.flush()


def test_database_accepts_none_and_valid_recurrence_pairs(
    clean_database: Engine,
) -> None:
    with session_scope(clean_database) as session:
        for public_code, repeat_unit, repeat_every in (
            ("valid-none", None, None),
            ("valid-days", "days", 3650),
            ("valid-months", "months", 120),
        ):
            session.add(
                HealthReminder(
                    profile_id=PROFILE_ID,
                    public_code=public_code,
                    title="Direct write",
                    reason="Constraint regression",
                    source_type="test",
                    source_reference="test",
                    due_at=datetime(2026, 9, 6, tzinfo=UTC),
                    timezone_name="UTC",
                    status=ReminderStatus.PENDING_CONFIRMATION.value,
                    delivery_revision=1,
                    repeat_unit=repeat_unit,
                    repeat_every=repeat_every,
                )
            )
        session.flush()


def test_active_query_is_bounded_ordered_and_profile_scoped(
    clean_database: Engine,
) -> None:
    base_due = datetime(2026, 9, 6, tzinfo=UTC)
    with session_scope(clean_database) as session:
        session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
        repository = ReminderRepository(session)
        for index in range(25):
            repository.propose(
                profile_id=PROFILE_ID,
                title=f"active-{index:02d}",
                reason="Active",
                source_type="test",
                source_reference="test",
                due_at=base_due + timedelta(days=index),
                timezone_name="UTC",
                public_code=f"active-{index:02d}",
            )
        for index in range(3):
            completed = repository.propose(
                profile_id=PROFILE_ID,
                title=f"completed-{index}",
                reason="Completed",
                source_type="test",
                source_reference="test",
                due_at=base_due - timedelta(days=index + 1),
                timezone_name="UTC",
                public_code=f"completed-{index}",
            )
            repository.confirm(PROFILE_ID, completed.public_code, now=base_due)
            repository.complete(PROFILE_ID, completed.public_code, now=base_due)
        repository.propose(
            profile_id=OTHER_PROFILE_ID,
            title="foreign-active",
            reason="Private",
            source_type="test",
            source_reference="test",
            due_at=base_due - timedelta(days=10),
            timezone_name="UTC",
            public_code="foreign-active",
        )

        active = repository.active(PROFILE_ID, limit=20)
        assert len(active) == 20
        assert [item.title for item in active] == [
            f"active-{index:02d}" for index in range(20)
        ]


def test_overflow_rejects_completion_before_parent_mutation(
    clean_database: Engine,
) -> None:
    due = datetime.max.replace(tzinfo=UTC)
    with session_scope(clean_database) as session:
        repository = ReminderRepository(session)
        parent = _repeating(repository, due=due, unit="days", every=1)
        with pytest.raises(ValueError, match="invalid_recurrence_date"):
            repository.complete(PROFILE_ID, parent.public_code, now=due)
        assert (
            repository.get(PROFILE_ID, parent.public_code).status
            is ReminderStatus.SCHEDULED
        )
        assert repository.successor(PROFILE_ID, parent.public_code) is None


def test_downgrade_refuses_to_erase_recurrence_state(clean_database: Engine) -> None:
    due = datetime(2026, 9, 6, 7, tzinfo=UTC)
    with session_scope(clean_database) as session:
        _repeating(ReminderRepository(session), due=due, unit="days", every=30)

    config = Config("alembic.ini")
    with (
        pytest.raises(DBAPIError, match="Refusing to downgrade reminders"),
        clean_database.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "0008_lab_extraction")
