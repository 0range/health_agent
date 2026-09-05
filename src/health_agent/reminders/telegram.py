"""Deterministic Telegram commands and user-facing reminder messages."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine

from health_agent.db import session_scope
from health_agent.reminders.models import Reminder
from health_agent.reminders.repository import (
    InvalidReminderTransition,
    ReminderNotFound,
    ReminderRepository,
)
from health_agent.reminders.time import parse_local_datetime, require_aware_utc
from health_agent.telegram.types import MessageContext

REMINDER_COMMANDS = frozenset(
    {
        "reminder_confirm",
        "reminder_cancel",
        "reminder_done",
        "reminder_snooze",
        "reminder_reschedule",
    }
)
_DURATION = re.compile(r"^(?P<amount>[1-9][0-9]*)(?P<unit>[mhdw])$")


class DatabaseReminderCommands:
    usage_text = (
        "Формат команды не распознан. Используйте команду из сообщения о "
        "напоминании без изменений."
    )
    unavailable_text = "Напоминание не найдено или эта команда уже недоступна."

    def __init__(self, engine: Engine, *, clock=lambda: datetime.now(UTC)) -> None:
        self._engine = engine
        self._clock = clock

    def handle(self, context: MessageContext, text: str) -> str | None:
        parts = text.strip().split()
        if not parts:
            return None
        command = parts[0]
        if not command.startswith("/"):
            return None
        name = command[1:].split("@", 1)[0].casefold()
        if name not in REMINDER_COMMANDS:
            return None
        try:
            return self._execute(
                context.profile_id,
                name,
                parts[1:],
                action_key=f"telegram:{context.bot_id}:{context.update_id}",
            )
        except (ReminderNotFound, InvalidReminderTransition):
            return self.unavailable_text
        except ValueError:
            return self.usage_text

    def _execute(
        self, profile_id: UUID, name: str, arguments: list[str], *, action_key: str
    ) -> str:
        expected = 2 if name in {"reminder_snooze", "reminder_reschedule"} else 1
        if len(arguments) != expected:
            raise ValueError("invalid_reminder_command")
        code = arguments[0]
        now = require_aware_utc(self._clock())
        with session_scope(self._engine) as session:
            repository = ReminderRepository(session)
            if name == "reminder_confirm":
                repository.confirm(profile_id, code, now=now, action_key=action_key)
                return "Напоминание подтверждено и поставлено в расписание."
            if name == "reminder_cancel":
                repository.cancel(profile_id, code, now=now, action_key=action_key)
                return "Напоминание отменено."
            if name == "reminder_done":
                repository.complete(profile_id, code, now=now, action_key=action_key)
                return "Напоминание отмечено как выполненное."
            if name == "reminder_snooze":
                reminder = repository.snooze(
                    profile_id,
                    code,
                    duration=parse_snooze_duration(arguments[1]),
                    now=now,
                    action_key=action_key,
                )
                return f"Напоминание перенесено на {_local_due(reminder)}."
            reminder = repository.get(profile_id, code)
            due_at = parse_local_datetime(arguments[1], reminder.timezone_name)
            moved = repository.reschedule(
                profile_id,
                code,
                due_at=due_at,
                timezone_name=reminder.timezone_name,
                now=now,
                action_key=action_key,
            )
            return f"Напоминание перенесено на {_local_due(moved)}."


def proposal_message(reminder: Reminder) -> str:
    return "\n".join(
        (
            f"Предлагаю напоминание: {_safe_line(reminder.title)}",
            f"Когда: {_local_due(reminder)}",
            f"Почему: {_safe_line(reminder.reason)}",
            (
                f"Источник: {_safe_line(reminder.source_type)} — "
                f"{_safe_line(reminder.source_reference)}"
            ),
            f"Подтвердить: /reminder_confirm {reminder.public_code}",
            f"Отменить: /reminder_cancel {reminder.public_code}",
        )
    )


def due_message(reminder: Reminder) -> str:
    return "\n".join(
        (
            f"Напоминание: {_safe_line(reminder.title)}",
            f"Почему: {_safe_line(reminder.reason)}",
            (
                f"Источник: {_safe_line(reminder.source_type)} — "
                f"{_safe_line(reminder.source_reference)}"
            ),
            f"Выполнено: /reminder_done {reminder.public_code}",
            f"Отложить на день: /reminder_snooze {reminder.public_code} 1d",
            (
                "Перенести на дату: "
                f"/reminder_reschedule {reminder.public_code} YYYY-MM-DDTHH:MM"
            ),
            f"Отменить: /reminder_cancel {reminder.public_code}",
        )
    )


def _local_due(reminder: Reminder) -> str:
    local = reminder.due_at.astimezone(ZoneInfo(reminder.timezone_name))
    return f"{local:%Y-%m-%d %H:%M} ({reminder.timezone_name})"


def parse_snooze_duration(value: str) -> timedelta:
    match = _DURATION.fullmatch(value.casefold())
    if match is None:
        raise ValueError("invalid_snooze_duration")
    amount = int(match.group("amount"))
    unit = match.group("unit")
    multipliers = {
        "m": timedelta(minutes=1),
        "h": timedelta(hours=1),
        "d": timedelta(days=1),
        "w": timedelta(weeks=1),
    }
    try:
        duration = amount * multipliers[unit]
    except OverflowError as error:
        raise ValueError("invalid_snooze_duration") from error
    if duration > timedelta(days=365):
        raise ValueError("invalid_snooze_duration")
    return duration


def _safe_line(value: str) -> str:
    return " ".join(value.split())
