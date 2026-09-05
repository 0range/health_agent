from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID

from sqlalchemy import Engine

from health_agent.db import session_scope
from health_agent.models import Profile
from health_agent.reminders.dispatcher import ReminderDispatcher
from health_agent.reminders.models import ReminderStatus
from health_agent.reminders.repository import ReminderRepository
from health_agent.reminders.telegram import DatabaseReminderCommands
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    MessageContext,
    TelegramGateway,
    TelegramIdentity,
)

BOT_ID = 111
PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
OTHER_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000002")


@dataclass
class Clock:
    value: datetime = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value


class Gateway:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)


def _context(profile_id: UUID, update_id: int, clock: Clock) -> MessageContext:
    chat_id = 101 if profile_id == PROFILE_ID else 202
    return MessageContext(
        BOT_ID,
        profile_id,
        chat_id,
        chat_id,
        update_id,
        update_id,
        clock.value,
        clock.value,
    )


def test_full_confirmed_reminder_flow_survives_dispatch_cycles_and_isolates_profiles(
    clean_database: Engine, tmp_path: Path
) -> None:
    clock = Clock()
    with session_scope(clean_database) as session:
        session.add(Profile(id=OTHER_PROFILE_ID, name="Other"))
        repository = ReminderRepository(session)
        first = repository.propose(
            profile_id=PROFILE_ID,
            title="Repeat ferritin",
            reason="Doctor requested a repeat",
            source_type="doctor_note",
            source_reference="document:abc",
            due_at=clock.value,
            timezone_name="Europe/Moscow",
            now=clock.value,
            public_code="first-code",
        )
        second = repository.propose(
            profile_id=OTHER_PROFILE_ID,
            title="Other private reminder",
            reason="Other private reason",
            source_type="user",
            source_reference="telegram",
            due_at=clock.value,
            timezone_name="UTC",
            now=clock.value,
            public_code="second-code",
        )

    state = SqliteTelegramState(tmp_path / "telegram.sqlite3", clock=clock)
    state.register_bot(BOT_ID, "health_bot")
    state.bind_identity(BOT_ID, TelegramIdentity(101, PROFILE_ID, 101))
    state.bind_identity(BOT_ID, TelegramIdentity(202, OTHER_PROFILE_ID, 202))
    gateway = Gateway()
    dispatcher = ReminderDispatcher(
        clean_database,
        TelegramMessenger(BOT_ID, cast(TelegramGateway, gateway), state),
        clock=clock,
    )
    commands = DatabaseReminderCommands(clean_database, clock=clock)

    proposal_report = dispatcher.run()
    assert proposal_report.proposals_sent == 2
    assert proposal_report.due_sent == 0
    assert (
        commands.handle(
            _context(PROFILE_ID, 1, clock), f"/reminder_confirm {second.public_code}"
        )
        == commands.unavailable_text
    )

    commands.handle(
        _context(PROFILE_ID, 2, clock), f"/reminder_confirm {first.public_code}"
    )
    first_due = dispatcher.run()
    assert first_due.due_sent == 1
    commands.handle(
        _context(PROFILE_ID, 3, clock), f"/reminder_snooze {first.public_code} 1d"
    )
    assert dispatcher.run().due_sent == 0

    clock.value += timedelta(days=1)
    redelivery = dispatcher.run()
    assert redelivery.due_sent == 1
    commands.handle(
        _context(PROFILE_ID, 4, clock), f"/reminder_done {first.public_code}"
    )
    assert dispatcher.run().due_sent == 0

    with session_scope(clean_database) as session:
        stored_first = ReminderRepository(session).get(PROFILE_ID, first.public_code)
        stored_second = ReminderRepository(session).get(
            OTHER_PROFILE_ID, second.public_code
        )
    assert stored_first.status is ReminderStatus.COMPLETED
    assert stored_first.delivery_revision == 2
    assert stored_second.status is ReminderStatus.PENDING_CONFIRMATION
    assert sum("Repeat ferritin" in text for _, text in gateway.sent) == 3
    assert sum("Other private reminder" in text for _, text in gateway.sent) == 1
