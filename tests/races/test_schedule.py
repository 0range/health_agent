from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from health_agent.races.schedule import next_check, next_scheduled_check

MOSCOW = ZoneInfo("Europe/Moscow")


def at(value):
    return datetime.fromisoformat(value).replace(tzinfo=MOSCOW)


@pytest.mark.parametrize("hour,previous_hour", [(8, 7), (13, 12), (18, 17)])
def test_each_fixed_slot_preempts_the_daily_interval(hour, previous_hour):
    now = at(f"2026-11-16T{hour:02}:00")
    previous = at(f"2026-11-16T{previous_hour:02}:59")
    assert next_check(now, previous, 86400) == now
    assert next_check(now, now, 86400) == next_scheduled_check(now)


def test_no_early_fixed_check_and_moscow_conversion():
    now = datetime.fromisoformat("2026-11-16T09:59:59+00:00")
    assert next_check(now, at("2026-11-16T08:00"), 86400) == at("2026-11-16T13:00")
    assert next_check(
        datetime.fromisoformat("2026-11-16T10:00+00:00"),
        at("2026-11-16T08:00"),
        86400,
    ) == at("2026-11-16T13:00")


def test_night_and_missed_slots_catch_up_once():
    now = at("2026-11-17T00:01")
    assert next_check(now, at("2026-11-16T18:00"), 86400) == at("2026-11-17T08:00")
    assert next_check(now, at("2026-11-15T18:00"), 86400) == now
    assert next_check(now, None, 86400) == now
    assert next_check(now, now, 86400) == at("2026-11-17T08:00")


@pytest.mark.parametrize("period", [3600, 300])
def test_intensified_and_fixed_triggers_coalesce(period):
    now = at("2026-11-16T13:00")
    assert next_check(now, at("2026-11-16T12:00"), period) == now
    result = next_check(now, now, period)
    assert (result - now).total_seconds() == period


def test_fixed_slot_can_be_earlier_than_next_hourly_check():
    assert next_check(at("2026-11-16T17:55"), at("2026-11-16T17:50"), 3600) == at(
        "2026-11-16T18:00"
    )


def test_interval_can_be_due_between_fixed_slots():
    now = at("2026-11-16T14:30")
    assert next_check(now, at("2026-11-16T13:00"), 3600) == now
