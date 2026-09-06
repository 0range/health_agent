from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Engine

from health_agent.db import session_scope
from health_agent.reminders.repository import ReminderRepository
from health_agent.reminders.telegram import DatabaseReminderCommands
from health_agent.telegram.types import MessageContext

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


def _context(profile_id: UUID, update_id: int = 30) -> MessageContext:
    return MessageContext(1, profile_id, 10, 10, 20, update_id, NOW, NOW)


def _proposal(engine: Engine) -> str:
    with session_scope(engine) as session:
        return (
            ReminderRepository(session)
            .propose(
                profile_id=PROFILE_ID,
                title="Repeat ferritin test",
                reason="Doctor requested a repeat",
                source_type="doctor_note",
                source_reference="document:abc",
                due_at=datetime(2026, 9, 6, 7, 0, tzinfo=UTC),
                timezone_name="Europe/Moscow",
                now=NOW,
                public_code="safe-code-1",
            )
            .public_code
        )


def test_exact_commands_mutate_only_bound_profile(clean_database: Engine) -> None:
    code = _proposal(clean_database)
    commands = DatabaseReminderCommands(clean_database, clock=lambda: NOW)

    confirmed = commands.handle(_context(PROFILE_ID, 1), f"/reminder_confirm {code}")
    denied = commands.handle(_context(OTHER_PROFILE_ID, 2), f"/reminder_done {code}")
    snooze_context = _context(PROFILE_ID, 3)
    snoozed = commands.handle(snooze_context, f"/reminder_snooze {code} 2h")
    repeated_snooze = commands.handle(snooze_context, f"/reminder_snooze {code} 2h")
    moved = commands.handle(
        _context(PROFILE_ID, 4), f"/reminder_reschedule {code} 2026-09-07T10:30"
    )
    done_context = _context(PROFILE_ID, 5)
    done = commands.handle(done_context, f"/reminder_done {code}")

    assert confirmed is not None and "подтверждено" in confirmed.casefold()
    assert denied == commands.unavailable_text
    assert snoozed is not None and "перенесено" in snoozed.casefold()
    assert repeated_snooze == snoozed
    assert moved is not None and "2026-09-07 10:30" in moved
    assert done is not None and "выполненн" in done.casefold()
    assert commands.handle(done_context, f"/reminder_done {code}") == done


def test_cancel_pending_and_reject_malformed_without_llm(
    clean_database: Engine,
) -> None:
    code = _proposal(clean_database)
    commands = DatabaseReminderCommands(clean_database, clock=lambda: NOW)

    assert commands.handle(_context(PROFILE_ID), "ordinary health question") is None
    assert (
        commands.handle(_context(PROFILE_ID), "/reminder_confirm")
        == commands.usage_text
    )
    assert (
        commands.handle(_context(PROFILE_ID), f"/reminder_snooze {code} forever")
        == commands.usage_text
    )
    cancelled = commands.handle(_context(PROFILE_ID), f"/reminder_cancel {code}")
    assert cancelled is not None and "отменено" in cancelled.casefold()
    assert (
        commands.handle(_context(PROFILE_ID), f"/reminder_confirm {code}")
        == commands.unavailable_text
    )


def test_new_list_confirm_done_and_cancel_recurring_chain(
    clean_database: Engine,
) -> None:
    commands = DatabaseReminderCommands(clean_database, clock=lambda: NOW)
    context = _context(PROFILE_ID, 101)

    proposal = commands.handle(
        context, "/reminder_new 2026-09-06T10:00 | Annual checkup | 12months"
    )
    replay = commands.handle(
        context, "/reminder_new 2026-09-06T10:00 | Annual checkup | 12months"
    )
    assert proposal == replay
    assert proposal is not None and "12" in proposal and "месяц" in proposal.casefold()

    with session_scope(clean_database) as session:
        reminders = ReminderRepository(session).list(PROFILE_ID)
        assert len(reminders) == 1
        parent = reminders[0]

    assert (
        commands.handle(context, "/reminder_new 2026-09-07T10:00 | Altered | 12months")
        == commands.unavailable_text
    )
    assert "Annual checkup" in (
        commands.handle(_context(PROFILE_ID, 102), "/reminders") or ""
    )
    commands.handle(
        _context(PROFILE_ID, 103), f"/reminder_confirm {parent.public_code}"
    )
    done = commands.handle(
        _context(PROFILE_ID, 104), f"/reminder_done {parent.public_code}"
    )
    assert done is not None and "отмен" in done.casefold()

    with session_scope(clean_database) as session:
        child = ReminderRepository(session).successor(PROFILE_ID, parent.public_code)
        assert child is not None
    commands.handle(_context(PROFILE_ID, 105), f"/reminder_cancel {child.public_code}")


def test_new_rejects_bad_syntax_and_list_is_bounded(clean_database: Engine) -> None:
    commands = DatabaseReminderCommands(clean_database, clock=lambda: NOW)
    assert (
        commands.handle(_context(PROFILE_ID), "/reminder_new tomorrow")
        == commands.new_usage_text
    )
    assert (
        commands.handle(_context(PROFILE_ID), "/reminders extra") == commands.usage_text
    )
