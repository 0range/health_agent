"""Immutable public contracts for the Google Calendar adapter."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import quote
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

CalendarStatus = Literal["created", "updated", "unchanged", "cancelled", "deferred"]


def _bounded(value: str, name: str, maximum: int) -> str:
    value = value.strip()
    if not value or len(value) > maximum:
        raise ValueError(f"invalid_{name}")
    return value


@dataclass(frozen=True, slots=True)
class CalendarEvent:
    profile_id: UUID
    visit_id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone_name: str
    questions: tuple[str, ...] = ()
    cancelled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "title", _bounded(self.title, "title", 200))
        if (
            self.starts_at.tzinfo is None
            or self.starts_at.utcoffset() is None
            or self.ends_at.tzinfo is None
            or self.ends_at.utcoffset() is None
            or self.ends_at <= self.starts_at
        ):
            raise ValueError("invalid_calendar_dates")
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as error:
            raise ValueError("invalid_timezone") from error
        if len(self.questions) > 20:
            raise ValueError("invalid_questions")
        object.__setattr__(
            self,
            "questions",
            tuple(_bounded(q, "question", 1000) for q in self.questions),
        )


@dataclass(frozen=True, slots=True)
class CalendarProfile:
    profile_id: UUID
    calendar_id: str = "primary"
    account_subject: str | None = None
    account_email: str | None = None
    enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "calendar_id", _bounded(self.calendar_id, "calendar_id", 500)
        )
        for name in ("account_subject", "account_email"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _bounded(value, name, 500))

    @property
    def encoded_calendar_id(self) -> str:
        return quote(self.calendar_id, safe="")


@dataclass(frozen=True, slots=True)
class CalendarResult:
    event_id: str
    status: CalendarStatus
    html_link: str | None = None
    safe_error: str | None = None
