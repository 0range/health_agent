"""Strict conversion between explicit IANA local time and UTC."""

from __future__ import annotations

import calendar
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone(timezone_name: str) -> ZoneInfo:
    name = timezone_name.strip()
    if not name:
        raise ValueError("invalid_timezone")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as error:
        raise ValueError("invalid_timezone") from error


def parse_local_datetime(value: str, timezone_name: str) -> datetime:
    """Parse an ISO timestamp and return UTC, rejecting unsafe wall times."""

    zone = validate_timezone(timezone_name)
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError("invalid_datetime") from error
    if parsed.tzinfo is not None:
        if parsed.utcoffset() != parsed.astimezone(zone).utcoffset():
            raise ValueError("offset_does_not_match_timezone")
        return parsed.astimezone(UTC)

    valid_candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(UTC).astimezone(zone)
        if round_trip.replace(tzinfo=None) == parsed and round_trip.fold == fold:
            valid_candidates.append(candidate)
    if not valid_candidates:
        raise ValueError("nonexistent_local_time")
    offsets = {candidate.utcoffset() for candidate in valid_candidates}
    if len(offsets) > 1:
        raise ValueError("ambiguous_local_time")
    return valid_candidates[0].astimezone(UTC)


def require_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime_must_be_timezone_aware")
    return value.astimezone(UTC)


def next_recurrence_due(
    previous_due: datetime,
    completed_at: datetime,
    timezone_name: str,
    repeat_unit: str,
    repeat_every: int,
) -> datetime:
    """Advance the later lifecycle instant by a calendar recurrence interval."""

    zone = validate_timezone(timezone_name)
    due = require_aware_utc(previous_due)
    completed = require_aware_utc(completed_at)
    if not (
        (repeat_unit == "days" and 1 <= repeat_every <= 3650)
        or (repeat_unit == "months" and 1 <= repeat_every <= 120)
    ):
        raise ValueError("invalid_recurrence")
    try:
        base = max(due, completed).astimezone(zone)
        wall = base.replace(tzinfo=None)
        if repeat_unit == "days":
            target = wall + timedelta(days=repeat_every)
        else:
            month_index = wall.year * 12 + wall.month - 1 + repeat_every
            year, zero_based_month = divmod(month_index, 12)
            month = zero_based_month + 1
            day = min(wall.day, calendar.monthrange(year, month)[1])
            target = wall.replace(year=year, month=month, day=day)
    except (OverflowError, ValueError) as error:
        raise ValueError("invalid_recurrence_date") from error
    return _resolve_recurrence_wall_time(target, zone)


def _resolve_recurrence_wall_time(wall: datetime, zone: ZoneInfo) -> datetime:
    for minute in range(181):
        candidate_wall = wall + timedelta(minutes=minute)
        candidates: list[datetime] = []
        for fold in (0, 1):
            candidate = candidate_wall.replace(tzinfo=zone, fold=fold)
            round_trip = candidate.astimezone(UTC).astimezone(zone)
            if round_trip.replace(tzinfo=None) == candidate_wall:
                candidates.append(candidate.astimezone(UTC))
        if candidates:
            return min(candidates)
    raise ValueError("invalid_recurrence_date")
