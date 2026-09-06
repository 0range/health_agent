"""Transactional profile-scoped reminder state machine."""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.models import Profile
from health_agent.reminders.models import (
    HealthReminder,
    HealthReminderEvent,
    Reminder,
    ReminderStatus,
    ReminderStatusSummary,
)
from health_agent.reminders.time import (
    next_recurrence_due,
    require_aware_utc,
    validate_timezone,
)

_PUBLIC_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,31}$")


class ReminderNotFound(LookupError):
    """No reminder with the profile/code pair exists."""


class InvalidReminderTransition(ValueError):
    """The requested lifecycle transition is not compatible with current state."""


def _now() -> datetime:
    return datetime.now(UTC)


class ReminderRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def propose(
        self,
        *,
        profile_id: UUID,
        title: str,
        reason: str,
        source_type: str,
        source_reference: str,
        due_at: datetime,
        timezone_name: str,
        now: datetime | None = None,
        public_code: str | None = None,
        repeat_unit: str | None = None,
        repeat_every: int | None = None,
    ) -> Reminder:
        recurrence = _validate_recurrence(repeat_unit, repeat_every)
        timestamp = require_aware_utc(now or _now())
        due = require_aware_utc(due_at)
        zone = validate_timezone(timezone_name)
        values = {
            "title": _bounded(title, "title", 500),
            "reason": _bounded(reason, "reason", 10_000),
            "source_type": _bounded(source_type, "source_type", 100),
            "source_reference": _bounded(source_reference, "source_reference", 2_000),
        }
        if self.session.get(Profile, profile_id) is None:
            raise ReminderNotFound("profile_or_reminder_not_found")
        code = _bounded(
            public_code or f"r{secrets.token_urlsafe(9)}", "public_code", 32
        )
        if _PUBLIC_CODE.fullmatch(code) is None:
            raise ValueError("invalid_public_code")
        row = HealthReminder(
            profile_id=profile_id,
            public_code=code,
            due_at=due,
            timezone_name=zone.key,
            status=ReminderStatus.PENDING_CONFIRMATION.value,
            confirmed_at=None,
            proposal_notified_at=None,
            delivered_at=None,
            completed_at=None,
            cancelled_at=None,
            delivery_revision=1,
            repeat_unit=recurrence[0],
            repeat_every=recurrence[1],
            recurrence_parent_id=None,
            created_at=timestamp,
            updated_at=timestamp,
            **values,
        )
        self.session.add(row)
        self.session.flush()
        self._event(row, "proposed", timestamp, {"due_at": due.isoformat()})
        self.session.flush()
        return _snapshot(row)

    def get(
        self, profile_id: UUID, public_code: str, *, lock: bool = False
    ) -> Reminder:
        return _snapshot(self._row(profile_id, public_code, lock=lock))

    def list(self, profile_id: UUID) -> tuple[Reminder, ...]:
        rows = self.session.scalars(
            select(HealthReminder)
            .where(HealthReminder.profile_id == profile_id)
            .order_by(HealthReminder.due_at, HealthReminder.id)
        ).all()
        return tuple(_snapshot(row) for row in rows)

    def successor(self, profile_id: UUID, public_code: str) -> Reminder | None:
        parent = self._row(profile_id, public_code, lock=False)
        row = self.session.scalar(
            select(HealthReminder).where(
                HealthReminder.profile_id == profile_id,
                HealthReminder.recurrence_parent_id == parent.id,
            )
        )
        return None if row is None else _snapshot(row)

    def confirm(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        now: datetime | None = None,
        action_key: str | None = None,
    ) -> Reminder:
        timestamp = require_aware_utc(now or _now())
        row = self._row(profile_id, public_code, lock=True)
        if self._action_replayed(row, "confirmed", action_key):
            return _snapshot(row)
        if row.status == ReminderStatus.SCHEDULED.value:
            return _snapshot(row)
        if row.status != ReminderStatus.PENDING_CONFIRMATION.value:
            raise InvalidReminderTransition("reminder_transition_not_allowed")
        row.status = ReminderStatus.SCHEDULED.value
        row.confirmed_at = timestamp
        row.updated_at = timestamp
        self._event(row, "confirmed", timestamp, action_key=action_key)
        self.session.flush()
        return _snapshot(row)

    def snooze(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        duration: timedelta,
        now: datetime | None = None,
        action_key: str | None = None,
    ) -> Reminder:
        if duration <= timedelta(0) or duration > timedelta(days=365):
            raise ValueError("invalid_snooze_duration")
        timestamp = require_aware_utc(now or _now())
        return self._move(
            profile_id,
            public_code,
            due_at=timestamp + duration,
            timezone_name=None,
            timestamp=timestamp,
            event_type="snoozed",
            action_key=action_key,
        )

    def reschedule(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        due_at: datetime,
        timezone_name: str,
        now: datetime | None = None,
        action_key: str | None = None,
    ) -> Reminder:
        timestamp = require_aware_utc(now or _now())
        return self._move(
            profile_id,
            public_code,
            due_at=require_aware_utc(due_at),
            timezone_name=validate_timezone(timezone_name).key,
            timestamp=timestamp,
            event_type="rescheduled",
            action_key=action_key,
        )

    def complete(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        now: datetime | None = None,
        action_key: str | None = None,
    ) -> Reminder:
        timestamp = require_aware_utc(now or _now())
        row = self._row(profile_id, public_code, lock=True)
        if self._action_replayed(row, "completed", action_key):
            return _snapshot(row)
        if row.status == ReminderStatus.COMPLETED.value:
            return _snapshot(row)
        if row.status != ReminderStatus.SCHEDULED.value:
            raise InvalidReminderTransition("reminder_transition_not_allowed")
        child_due = (
            next_recurrence_due(
                row.due_at,
                timestamp,
                row.timezone_name,
                row.repeat_unit,
                row.repeat_every,
            )
            if row.repeat_unit is not None and row.repeat_every is not None
            else None
        )
        row.status = ReminderStatus.COMPLETED.value
        row.completed_at = timestamp
        row.updated_at = timestamp
        self._event(row, "completed", timestamp, action_key=action_key)
        if child_due is not None:
            child = HealthReminder(
                profile_id=row.profile_id,
                public_code=f"r{secrets.token_urlsafe(9)}",
                title=row.title,
                reason=row.reason,
                source_type=row.source_type,
                source_reference=row.source_reference,
                due_at=child_due,
                timezone_name=row.timezone_name,
                status=ReminderStatus.SCHEDULED.value,
                confirmed_at=timestamp,
                proposal_notified_at=None,
                delivered_at=None,
                completed_at=None,
                cancelled_at=None,
                delivery_revision=1,
                repeat_unit=row.repeat_unit,
                repeat_every=row.repeat_every,
                recurrence_parent_id=row.id,
                created_at=timestamp,
                updated_at=timestamp,
            )
            self.session.add(child)
            self.session.flush()
            self._event(
                child,
                "proposed",
                timestamp,
                {
                    "due_at": child.due_at.isoformat(),
                    "recurrence_parent_id": str(row.id),
                },
            )
            self._event(child, "confirmed", timestamp)
        self.session.flush()
        return _snapshot(row)

    def cancel(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        now: datetime | None = None,
        action_key: str | None = None,
    ) -> Reminder:
        timestamp = require_aware_utc(now or _now())
        row = self._row(profile_id, public_code, lock=True)
        if self._action_replayed(row, "cancelled", action_key):
            return _snapshot(row)
        if row.status == ReminderStatus.CANCELLED.value:
            return _snapshot(row)
        if row.status not in {
            ReminderStatus.PENDING_CONFIRMATION.value,
            ReminderStatus.SCHEDULED.value,
        }:
            raise InvalidReminderTransition("reminder_transition_not_allowed")
        row.status = ReminderStatus.CANCELLED.value
        row.cancelled_at = timestamp
        row.updated_at = timestamp
        self._event(row, "cancelled", timestamp, action_key=action_key)
        self.session.flush()
        return _snapshot(row)

    def pending_proposals(self, limit: int = 100) -> tuple[Reminder, ...]:
        rows = self.session.scalars(
            select(HealthReminder)
            .where(
                HealthReminder.status == ReminderStatus.PENDING_CONFIRMATION.value,
                HealthReminder.proposal_notified_at.is_(None),
            )
            .order_by(HealthReminder.created_at, HealthReminder.id)
            .limit(_limit(limit))
        ).all()
        return tuple(_snapshot(row) for row in rows)

    def due_occurrences(self, now: datetime, limit: int = 100) -> tuple[Reminder, ...]:
        timestamp = require_aware_utc(now)
        rows = self.session.scalars(
            select(HealthReminder)
            .where(
                HealthReminder.status == ReminderStatus.SCHEDULED.value,
                HealthReminder.confirmed_at.is_not(None),
                HealthReminder.due_at <= timestamp,
                HealthReminder.delivered_at.is_(None),
            )
            .order_by(HealthReminder.due_at, HealthReminder.id)
            .limit(_limit(limit))
        ).all()
        return tuple(_snapshot(row) for row in rows)

    def pending_proposal_for_delivery(
        self, profile_id: UUID, reminder_id: UUID
    ) -> Reminder | None:
        """Lock and revalidate a proposal immediately before external delivery."""

        row = self._row_by_id(profile_id, reminder_id, lock=True)
        if (
            row.status != ReminderStatus.PENDING_CONFIRMATION.value
            or row.proposal_notified_at is not None
        ):
            return None
        return _snapshot(row)

    def due_occurrence_for_delivery(
        self,
        profile_id: UUID,
        reminder_id: UUID,
        *,
        delivery_revision: int,
        now: datetime,
    ) -> Reminder | None:
        """Lock and revalidate one occurrence, fencing stale revisions."""

        timestamp = require_aware_utc(now)
        row = self._row_by_id(profile_id, reminder_id, lock=True)
        if (
            row.status != ReminderStatus.SCHEDULED.value
            or row.confirmed_at is None
            or row.delivery_revision != delivery_revision
            or row.due_at > timestamp
            or row.delivered_at is not None
        ):
            return None
        return _snapshot(row)

    def mark_proposal_notified(
        self, profile_id: UUID, reminder_id: UUID, *, notified_at: datetime
    ) -> bool:
        timestamp = require_aware_utc(notified_at)
        row = self._row_by_id(profile_id, reminder_id, lock=True)
        if row.status != ReminderStatus.PENDING_CONFIRMATION.value:
            return False
        if row.proposal_notified_at is not None:
            return True
        row.proposal_notified_at = timestamp
        row.updated_at = timestamp
        self._event(row, "proposal_notified", timestamp)
        self.session.flush()
        return True

    def mark_due_delivered(
        self,
        profile_id: UUID,
        reminder_id: UUID,
        *,
        delivery_revision: int,
        delivered_at: datetime,
    ) -> bool:
        timestamp = require_aware_utc(delivered_at)
        row = self._row_by_id(profile_id, reminder_id, lock=True)
        if (
            row.status != ReminderStatus.SCHEDULED.value
            or row.delivery_revision != delivery_revision
        ):
            return False
        if row.delivered_at is not None:
            return True
        row.delivered_at = timestamp
        row.updated_at = timestamp
        self._event(
            row, "delivered", timestamp, {"delivery_revision": delivery_revision}
        )
        self.session.flush()
        return True

    def status(
        self, profile_id: UUID, *, now: datetime | None = None
    ) -> ReminderStatusSummary:
        timestamp = require_aware_utc(now or _now())
        rows = self.session.scalars(
            select(HealthReminder).where(HealthReminder.profile_id == profile_id)
        ).all()
        statuses = [row.status for row in rows]
        return ReminderStatusSummary(
            total=len(rows),
            pending_confirmation=statuses.count(
                ReminderStatus.PENDING_CONFIRMATION.value
            ),
            scheduled=statuses.count(ReminderStatus.SCHEDULED.value),
            due=sum(
                row.status == ReminderStatus.SCHEDULED.value
                and row.confirmed_at is not None
                and row.delivered_at is None
                and row.due_at <= timestamp
                for row in rows
            ),
            delivered=sum(row.delivered_at is not None for row in rows),
            completed=statuses.count(ReminderStatus.COMPLETED.value),
            cancelled=statuses.count(ReminderStatus.CANCELLED.value),
        )

    def _move(
        self,
        profile_id: UUID,
        public_code: str,
        *,
        due_at: datetime,
        timezone_name: str | None,
        timestamp: datetime,
        event_type: str,
        action_key: str | None,
    ) -> Reminder:
        row = self._row(profile_id, public_code, lock=True)
        if self._action_replayed(row, event_type, action_key):
            return _snapshot(row)
        if row.status != ReminderStatus.SCHEDULED.value:
            raise InvalidReminderTransition("reminder_transition_not_allowed")
        target_zone = timezone_name or row.timezone_name
        if (
            row.due_at == due_at
            and row.timezone_name == target_zone
            and row.delivered_at is None
        ):
            return _snapshot(row)
        row.due_at = due_at
        row.timezone_name = target_zone
        row.delivered_at = None
        row.delivery_revision += 1
        row.updated_at = timestamp
        self._event(
            row,
            event_type,
            timestamp,
            {"due_at": due_at.isoformat(), "timezone": target_zone},
            action_key=action_key,
        )
        self.session.flush()
        return _snapshot(row)

    def _row(self, profile_id: UUID, public_code: str, *, lock: bool) -> HealthReminder:
        statement = select(HealthReminder).where(
            HealthReminder.profile_id == profile_id,
            HealthReminder.public_code == public_code,
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise ReminderNotFound("profile_or_reminder_not_found")
        return row

    def _row_by_id(
        self, profile_id: UUID, reminder_id: UUID, *, lock: bool
    ) -> HealthReminder:
        statement = select(HealthReminder).where(
            HealthReminder.profile_id == profile_id,
            HealthReminder.id == reminder_id,
        )
        if lock:
            statement = statement.with_for_update()
        row = self.session.scalar(statement)
        if row is None:
            raise ReminderNotFound("profile_or_reminder_not_found")
        return row

    def _event(
        self,
        row: HealthReminder,
        event_type: str,
        occurred_at: datetime,
        data: dict[str, object] | None = None,
        *,
        action_key: str | None = None,
    ) -> None:
        self.session.add(
            HealthReminderEvent(
                reminder_id=row.id,
                profile_id=row.profile_id,
                event_type=event_type,
                action_key=_action_key(action_key),
                event_data=data or {},
                occurred_at=occurred_at,
            )
        )

    def _action_replayed(
        self, row: HealthReminder, event_type: str, action_key: str | None
    ) -> bool:
        normalized = _action_key(action_key)
        if normalized is None:
            return False
        previous = self.session.scalar(
            select(HealthReminderEvent.event_type).where(
                HealthReminderEvent.profile_id == row.profile_id,
                HealthReminderEvent.reminder_id == row.id,
                HealthReminderEvent.action_key == normalized,
            )
        )
        if previous is None:
            return False
        if previous != event_type:
            raise InvalidReminderTransition("reminder_action_conflict")
        return True


def _snapshot(row: HealthReminder) -> Reminder:
    return Reminder(
        id=row.id,
        profile_id=row.profile_id,
        public_code=row.public_code,
        title=row.title,
        reason=row.reason,
        source_type=row.source_type,
        source_reference=row.source_reference,
        due_at=row.due_at,
        timezone_name=row.timezone_name,
        status=ReminderStatus(row.status),
        confirmed_at=row.confirmed_at,
        proposal_notified_at=row.proposal_notified_at,
        delivered_at=row.delivered_at,
        completed_at=row.completed_at,
        cancelled_at=row.cancelled_at,
        delivery_revision=row.delivery_revision,
        repeat_unit=row.repeat_unit,
        repeat_every=row.repeat_every,
        recurrence_parent_id=row.recurrence_parent_id,
    )


def _bounded(value: str, name: str, maximum: int) -> str:
    result = value.strip()
    if not result or len(result) > maximum:
        raise ValueError(f"invalid_{name}")
    return result


def _action_key(value: str | None) -> str | None:
    if value is None:
        return None
    return _bounded(value, "action_key", 200)


def _limit(value: int) -> int:
    if value < 1 or value > 1_000:
        raise ValueError("invalid_limit")
    return value


def _validate_recurrence(
    repeat_unit: str | None, repeat_every: int | None
) -> tuple[str | None, int | None]:
    if repeat_unit is None and repeat_every is None:
        return None, None
    if repeat_unit == "days" and repeat_every is not None and 1 <= repeat_every <= 3650:
        return repeat_unit, repeat_every
    if (
        repeat_unit == "months"
        and repeat_every is not None
        and 1 <= repeat_every <= 120
    ):
        return repeat_unit, repeat_every
    raise ValueError("invalid_recurrence")
