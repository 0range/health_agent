from __future__ import annotations

from datetime import UTC, datetime

import pytest

from health_agent.reminders.time import parse_local_datetime, validate_timezone


def test_naive_local_time_is_converted_using_explicit_zone() -> None:
    assert parse_local_datetime("2026-09-05T10:30", "Europe/Moscow") == datetime(
        2026, 9, 5, 7, 30, tzinfo=UTC
    )


def test_aware_time_must_match_explicit_zone_offset() -> None:
    assert parse_local_datetime(
        "2026-09-05T10:30:00+03:00", "Europe/Moscow"
    ) == datetime(2026, 9, 5, 7, 30, tzinfo=UTC)
    with pytest.raises(ValueError, match="offset_does_not_match_timezone"):
        parse_local_datetime("2026-09-05T10:30:00+02:00", "Europe/Moscow")


@pytest.mark.parametrize(
    ("value", "zone", "message"),
    [
        ("2026-03-29T02:30", "Europe/Berlin", "nonexistent_local_time"),
        ("2026-10-25T02:30", "Europe/Berlin", "ambiguous_local_time"),
        ("not-a-date", "Europe/Moscow", "invalid_datetime"),
    ],
)
def test_rejects_unsafe_local_times(value: str, zone: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_local_datetime(value, zone)


def test_rejects_unknown_or_blank_timezone() -> None:
    with pytest.raises(ValueError, match="invalid_timezone"):
        validate_timezone("Mars/Olympus")
    with pytest.raises(ValueError, match="invalid_timezone"):
        validate_timezone("  ")
