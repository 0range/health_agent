from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.whoop.dashboard import whoop_card_specs
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)


def test_generated_queries_select_latest_valid_profile_rows(
    session: Session,
) -> None:
    second_profile = Profile(id=uuid4(), name="Second")
    session.add(second_profile)
    session.flush()
    first = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 101, ("read:cycles", "read:sleep")
    )
    second = register_authorized_connection(
        session, second_profile.id, "main", 101, ("read:cycles", "read:sleep")
    )

    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=100,
        start="2020-01-01T01:00:00Z",
        updated_at="2020-01-01T01:00:00Z",
        strain=8,
        recovery_score=50,
        hrv=40,
        resting_hr=60,
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=101,
        start="2020-01-01T01:00:00Z",
        updated_at="2020-01-01T02:00:00Z",
        strain=12,
        recovery_score=70,
        hrv=45,
        resting_hr=65,
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=102,
        start="2020-01-01T03:00:00Z",
        updated_at="2020-01-01T03:00:00Z",
        strain=22,
        recovery_score=101,
        hrv=55,
        resting_hr="Infinity",
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=103,
        start="2020-01-02T01:00:00Z",
        updated_at="2020-01-02T01:00:00Z",
        strain=None,
        recovery_score=None,
        hrv=0,
        resting_hr=-1,
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=104,
        start="2020-01-03T01:00:00Z",
        updated_at="2020-01-03T01:00:00Z",
        strain=14,
        recovery_score=80,
        hrv=50,
        resting_hr=55,
        cycle_state="PENDING_SCORE",
        recovery_state="PENDING_SCORE",
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=105,
        start="2020-01-04T01:00:00Z",
        updated_at="2020-01-04T01:00:00Z",
        strain=None,
        recovery_score=None,
        hrv="NaN",
        resting_hr="-Infinity",
    )
    _store_cycle_with_recovery(
        session,
        second,
        cycle_id=100,
        start="2020-01-01T01:00:00Z",
        updated_at="2020-01-01T01:00:00Z",
        strain=3,
        recovery_score=10,
        hrv=20,
        resting_hr=50,
    )
    _store_cycle_with_recovery(
        session,
        first,
        cycle_id=200,
        start="2999-01-01T01:00:00Z",
        updated_at="2999-01-01T01:00:00Z",
        strain=15,
        recovery_score=80,
        hrv=50,
        resting_hr=55,
    )
    session.flush()

    results = {
        spec.metrics[0]: session.execute(text(spec.query)).mappings().all()
        for spec in whoop_card_specs(DEFAULT_PROFILE_ID)[:4]
    }

    assert _dated_values(results["recovery_score"], "recovery_score") == [
        (date(2020, 1, 1), Decimal(70))
    ]
    assert _dated_values(results["strain"], "strain") == [
        (date(2020, 1, 1), Decimal(12))
    ]
    assert _values(results["hrv_rmssd_milli"], "hrv_rmssd_milli") == [Decimal(55)]
    assert _values(results["resting_heart_rate"], "resting_heart_rate") == [Decimal(65)]
    assert all(tuple(rows[0]) == ("date", metric) for metric, rows in results.items())
    assert all(len(rows) == 1 for rows in results.values())

    second_recovery = whoop_card_specs(second_profile.id)[0]
    assert _values(
        session.execute(text(second_recovery.query)).mappings().all(),
        "recovery_score",
    ) == [Decimal(10)]
    session.rollback()


def test_sleep_queries_apply_validity_per_metric_and_exclude_naps(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 301, ("read:sleep",)
    )
    _store_sleep(
        session,
        connection,
        sleep_id="sleep-old",
        start="2020-01-05T01:00:00Z",
        updated_at="2020-01-05T01:00:00Z",
        total_sleep_milli=21_600_000,
        performance=70,
        efficiency=80,
    )
    _store_sleep(
        session,
        connection,
        sleep_id="sleep-newer-revision",
        start="2020-01-05T01:00:00Z",
        updated_at="2020-01-05T02:00:00Z",
        total_sleep_milli=25_200_000,
        performance=72,
        efficiency=82,
    )
    _store_sleep(
        session,
        connection,
        sleep_id="sleep-latest-partly-invalid",
        start="2020-01-05T03:00:00Z",
        updated_at="2020-01-05T03:00:00Z",
        total_sleep_milli=0,
        performance=75,
        efficiency="NaN",
    )
    _store_sleep(
        session,
        connection,
        sleep_id="outside-bounds",
        start="2020-01-06T01:00:00Z",
        updated_at="2020-01-06T01:00:00Z",
        total_sleep_milli=90_000_000,
        performance=101,
        efficiency=-1,
    )
    _store_sleep(
        session,
        connection,
        sleep_id="missing",
        start="2020-01-07T01:00:00Z",
        updated_at="2020-01-07T01:00:00Z",
        total_sleep_milli=None,
        performance=None,
        efficiency=None,
    )
    _store_sleep(
        session,
        connection,
        sleep_id="unscored",
        start="2020-01-08T01:00:00Z",
        updated_at="2020-01-08T01:00:00Z",
        total_sleep_milli=28_800_000,
        performance=90,
        efficiency=90,
        score_state="PENDING_SCORE",
    )
    _store_sleep(
        session,
        connection,
        sleep_id="nap",
        start="2020-01-09T01:00:00Z",
        updated_at="2020-01-09T01:00:00Z",
        total_sleep_milli=3_600_000,
        performance=99,
        efficiency=99,
        nap=True,
    )
    _store_sleep(
        session,
        connection,
        sleep_id="future",
        start="2999-01-02T01:00:00Z",
        updated_at="2999-01-02T01:00:00Z",
        total_sleep_milli=28_800_000,
        performance=90,
        efficiency=90,
    )
    session.flush()

    sleep_specs = whoop_card_specs(DEFAULT_PROFILE_ID)[4:7]
    results = {
        spec.metrics[0]: session.execute(text(spec.query)).mappings().all()
        for spec in sleep_specs
    }

    assert _dated_values(results["sleep_hours"], "sleep_hours") == [
        (date(2020, 1, 5), Decimal(7))
    ]
    assert _dated_values(
        results["sleep_performance_percentage"], "sleep_performance_percentage"
    ) == [(date(2020, 1, 5), Decimal(75))]
    assert _dated_values(
        results["sleep_efficiency_percentage"], "sleep_efficiency_percentage"
    ) == [(date(2020, 1, 5), Decimal(82))]
    assert all(tuple(rows[0]) == ("date", metric) for metric, rows in results.items())
    session.rollback()


def test_weight_is_one_latest_valid_snapshot_and_empty_profile_is_empty(
    session: Session,
) -> None:
    first = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "first", 201, ("read:body_measurement",)
    )
    second = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "second", 202, ("read:body_measurement",)
    )
    invalid = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "invalid", 203, ("read:body_measurement",)
    )
    expected_at = datetime(2020, 1, 2, tzinfo=UTC)
    _store_body(session, first, 70, expected_at)
    _store_body(session, second, 71, expected_at)
    _store_body(session, invalid, "Infinity", datetime(2020, 1, 3, tzinfo=UTC))
    session.flush()

    weight = next(
        spec for spec in whoop_card_specs(DEFAULT_PROFILE_ID) if spec.display == "table"
    )
    rows = session.execute(text(weight.query)).mappings().all()

    assert len(rows) == 1
    expected_weight = Decimal(70) if first.id > second.id else Decimal(71)
    assert tuple(rows[0]) == ("weight_kilogram", "observed_at")
    assert rows[0]["weight_kilogram"] == expected_weight
    assert rows[0]["observed_at"] == expected_at
    assert "ORDER BY observed_at DESC, connection_id DESC LIMIT 1" in weight.query

    empty_profile = uuid4()
    assert all(
        session.execute(text(spec.query)).all() == []
        for spec in whoop_card_specs(empty_profile)
    )
    session.rollback()


def _values(rows: list[Any], metric: str) -> list[Decimal]:
    return [row[metric] for row in rows]


def _dated_values(rows: list[Any], metric: str) -> list[tuple[date, Decimal]]:
    return [(row["date"], row[metric]) for row in rows]


def _store_cycle_with_recovery(
    session: Session,
    connection: WhoopConnection,
    *,
    cycle_id: int,
    start: str,
    updated_at: str,
    strain: int | None,
    recovery_score: int | None,
    hrv: int | str | None,
    resting_hr: int | str | None,
    cycle_state: str = "SCORED",
    recovery_state: str = "SCORED",
) -> None:
    cycle = {
        "id": cycle_id,
        "user_id": connection.external_user_id,
        "updated_at": updated_at,
        "start": start,
        "timezone_offset": "+00:00",
        "score_state": cycle_state,
        "score": {"strain": strain},
    }
    recovery = {
        "cycle_id": cycle_id,
        "user_id": connection.external_user_id,
        "updated_at": updated_at,
        "score_state": recovery_state,
        "score": {
            "recovery_score": recovery_score,
            "hrv_rmssd_milli": hrv,
            "resting_heart_rate": resting_hr,
        },
    }
    _store(session, connection, "cycle", cycle)
    _store(session, connection, "recovery", recovery)


def _store_sleep(
    session: Session,
    connection: WhoopConnection,
    *,
    sleep_id: str,
    start: str,
    updated_at: str,
    total_sleep_milli: int | None,
    performance: int | None,
    efficiency: int | str | None,
    nap: bool = False,
    score_state: str = "SCORED",
) -> None:
    payload = {
        "id": sleep_id,
        "user_id": connection.external_user_id,
        "updated_at": updated_at,
        "start": start,
        "timezone_offset": "+00:00",
        "nap": nap,
        "score_state": score_state,
        "score": {
            "sleep_performance_percentage": performance,
            "sleep_efficiency_percentage": efficiency,
            "stage_summary": _stage_summary(total_sleep_milli),
        },
    }
    _store(session, connection, "sleep", payload)


def _stage_summary(total_sleep_milli: int | None) -> dict[str, int] | None:
    if total_sleep_milli is None:
        return None
    return {
        "total_light_sleep_time_milli": total_sleep_milli,
        "total_slow_wave_sleep_time_milli": 0,
        "total_rem_sleep_time_milli": 0,
    }


def _store_body(
    session: Session,
    connection: WhoopConnection,
    weight: int | str,
    fetched_at: datetime,
) -> None:
    payload = {"weight_kilogram": weight}
    _store(session, connection, "body", payload, fetched_at=fetched_at)


def _store(
    session: Session,
    connection: WhoopConnection,
    kind: str,
    payload: dict[str, Any],
    *,
    fetched_at: datetime = datetime(2020, 1, 1, tzinfo=UTC),
) -> None:
    store_normalized_record(
        session,
        connection,
        normalize_whoop(kind, payload),
        payload,
        fetched_at,
    )
