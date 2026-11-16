"""Fixed Moscow checks combined with the existing opening-window cadence."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
SCHEDULED_HOURS = (8, 13, 18)


def slots_around(now: datetime) -> list[datetime]:
    local = now.astimezone(MOSCOW)
    return [
        local.replace(hour=hour, minute=0, second=0, microsecond=0)
        + timedelta(days=offset)
        for offset in (-1, 0, 1)
        for hour in SCHEDULED_HOURS
    ]


def next_scheduled_check(now: datetime) -> datetime:
    return min(slot for slot in slots_around(now) if slot > now)


def next_check(now: datetime, previous: datetime | None, period: int) -> datetime:
    """Use completed scan start time to catch up once after a missed fixed slot."""
    local = now.astimezone(MOSCOW)
    latest = max(slot for slot in slots_around(local) if slot <= local)
    if previous is None or previous < latest:
        return local
    return max(
        local,
        min(next_scheduled_check(local), previous + timedelta(seconds=period)),
    ).astimezone(MOSCOW)
