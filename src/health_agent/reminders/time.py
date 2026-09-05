"""Strict conversion between explicit IANA local time and UTC."""

from __future__ import annotations

from datetime import UTC, datetime
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
