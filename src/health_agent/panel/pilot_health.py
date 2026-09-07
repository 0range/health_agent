"""Read-only local Telegram pilot health without initializing state stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from health_agent.panel.models import BotStatus

_LABELS = {"main": "Основной", "food": "Питание", "training": "Тренировки"}


class TelegramBotHealthReader:
    """Read the three existing bot databases strictly through SQLite mode=ro."""

    def __init__(
        self,
        state_paths: Mapping[str, Path],
        token_paths: Mapping[str, Path | None] | None = None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._state_paths = dict(state_paths)
        self._token_paths = dict(token_paths or {})
        self._clock = clock

    def statuses(self, profile_id: UUID) -> tuple[BotStatus, ...]:
        return tuple(self._status(domain, profile_id) for domain in _LABELS)

    def _status(self, domain: str, profile_id: UUID) -> BotStatus:
        path = self._state_paths.get(domain)
        configured = bool(
            (token_path := self._token_paths.get(domain)) is not None
            and token_path.is_file()
        )
        if path is None or not path.is_file():
            return BotStatus(domain, _LABELS[domain], "unavailable", configured, False)
        try:
            uri = f"file:{quote(str(path.absolute()))}?mode=ro"
            with sqlite3.connect(uri, uri=True) as connection:
                row = connection.execute(
                    """SELECT n.bot_id, r.last_poll_at, r.last_error_code,
                              EXISTS(SELECT 1 FROM identities i WHERE i.bot_id=n.bot_id
                                  AND i.profile_id=? AND i.active=1) AS bound
                       FROM bot_namespaces n LEFT JOIN runtimes r ON r.bot_id=n.bot_id
                       ORDER BY bound DESC, n.bot_id LIMIT 1""",
                    (str(profile_id),),
                ).fetchone()
        except (OSError, sqlite3.Error):
            return BotStatus(domain, _LABELS[domain], "unavailable", configured, False)
        if row is None:
            return BotStatus(domain, _LABELS[domain], "unavailable", configured, False)
        bound = bool(row[3])
        if not bound:
            return BotStatus(domain, _LABELS[domain], "not_bound", configured, False)
        polled_at = _aware_datetime(row[1])
        if polled_at is None:
            status = "unknown"
        else:
            age = (self._clock().astimezone(UTC) - polled_at).total_seconds()
            if age < 0:
                status = "future"
            elif age <= 120 and row[2] is None:
                status = "fresh"
            else:
                status = "stale"
        return BotStatus(domain, _LABELS[domain], status, configured, True, polled_at)


def _aware_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
