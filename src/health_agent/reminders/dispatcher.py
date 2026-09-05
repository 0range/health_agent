"""Restart-safe one-shot delivery of proposals and confirmed occurrences."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.reminders.models import Reminder
from health_agent.reminders.repository import ReminderRepository
from health_agent.reminders.telegram import due_message, proposal_message
from health_agent.reminders.time import require_aware_utc
from health_agent.telegram.messenger import TelegramMessenger


@dataclass(frozen=True, slots=True)
class DispatchReport:
    proposals_sent: int = 0
    proposals_acknowledged: int = 0
    due_sent: int = 0
    due_acknowledged: int = 0
    failed: int = 0


class ReminderDispatcher:
    def __init__(
        self,
        engine: Engine,
        messenger: TelegramMessenger,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        repository_factory: Callable[
            [Session], ReminderRepository
        ] = ReminderRepository,
    ) -> None:
        self._engine = engine
        self._messenger = messenger
        self._clock = clock
        self._repository_factory = repository_factory

    def run(self, *, limit: int = 100) -> DispatchReport:
        now = require_aware_utc(self._clock())
        with session_scope(self._engine) as session:
            repository = self._repository_factory(session)
            proposals = repository.pending_proposals(limit)
            due = repository.due_occurrences(now, limit)
        proposal_sent = proposal_ack = due_sent = due_ack = failed = 0
        for candidate in proposals:
            sent, acknowledged, did_fail = self._deliver_proposal(candidate, now)
            proposal_sent += sent
            proposal_ack += acknowledged
            failed += did_fail
        for candidate in due:
            sent, acknowledged, did_fail = self._deliver_due(candidate, now)
            due_sent += sent
            due_ack += acknowledged
            failed += did_fail
        return DispatchReport(proposal_sent, proposal_ack, due_sent, due_ack, failed)

    def _deliver_proposal(
        self, candidate: Reminder, now: datetime
    ) -> tuple[int, int, int]:
        try:
            with session_scope(self._engine) as session:
                repository = self._repository_factory(session)
                reminder = repository.pending_proposal_for_delivery(
                    candidate.profile_id, candidate.id
                )
                if reminder is None:
                    return 0, 0, 0
                report = self._messenger.send_to_profile(
                    reminder.profile_id,
                    proposal_message(reminder),
                    delivery_key=f"health-reminder:proposal:{reminder.id}",
                )
                if not repository.mark_proposal_notified(
                    reminder.profile_id, reminder.id, notified_at=now
                ):
                    raise RuntimeError("proposal_ack_rejected")
                return int(report.sent > 0), int(report.previously_sent > 0), 0
        except Exception:  # noqa: BLE001 -- isolate profiles; expose counts only
            return 0, 0, 1

    def _deliver_due(self, candidate: Reminder, now: datetime) -> tuple[int, int, int]:
        try:
            with session_scope(self._engine) as session:
                repository = self._repository_factory(session)
                reminder = repository.due_occurrence_for_delivery(
                    candidate.profile_id,
                    candidate.id,
                    delivery_revision=candidate.delivery_revision,
                    now=now,
                )
                if reminder is None:
                    return 0, 0, 0
                report = self._messenger.send_to_profile(
                    reminder.profile_id,
                    due_message(reminder),
                    delivery_key=(
                        f"health-reminder:due:{reminder.id}:"
                        f"{reminder.delivery_revision}"
                    ),
                )
                if not repository.mark_due_delivered(
                    reminder.profile_id,
                    reminder.id,
                    delivery_revision=reminder.delivery_revision,
                    delivered_at=now,
                ):
                    raise RuntimeError("due_ack_rejected")
                return int(report.sent > 0), int(report.previously_sent > 0), 0
        except Exception:  # noqa: BLE001 -- isolate profiles; expose counts only
            return 0, 0, 1
