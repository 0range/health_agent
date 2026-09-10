"""The agreed pilot calendar, independent of the computer's local timezone."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Moscow")
STUDY_START = date(2026, 9, 10)


def bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, ZONE).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, ZONE).astimezone(UTC),
    )


def recent_days(now: datetime, *, include_today: bool = False) -> list[date]:
    today = now.astimezone(ZONE).date()
    return [
        day
        for offset in range(3, -1 if include_today else 0, -1)
        if (day := today - timedelta(days=offset)) >= STUDY_START
    ]
