from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from health_agent.whoop.normalize import (
    WhoopNormalizationError,
    canonical_payload_hash,
    normalize_whoop,
)


def test_payload_hash_is_independent_of_object_key_order() -> None:
    assert canonical_payload_hash({"b": 2, "a": 1}) == canonical_payload_hash(
        {"a": 1, "b": 2}
    )


def test_recovery_normalizes_all_official_health_metrics() -> None:
    result = normalize_whoop(
        "recovery",
        {
            "cycle_id": 93845,
            "sleep_id": "sleep-uuid",
            "user_id": 10129,
            "updated_at": "2022-04-24T14:25:44.774Z",
            "score_state": "SCORED",
            "score": {
                "user_calibrating": False,
                "recovery_score": 44,
                "resting_heart_rate": 64,
                "hrv_rmssd_milli": 31.813562,
                "spo2_percentage": 95.6875,
                "skin_temp_celsius": 33.7,
            },
        },
    )

    assert result.external_id == "93845"
    assert result.values["recovery_score"] == Decimal(44)
    assert result.values["hrv_rmssd_milli"] == Decimal("31.813562")
    assert result.values["spo2_percentage"] == Decimal("95.6875")
    assert result.values["skin_temp_celsius"] == Decimal("33.7")


def test_unscored_sleep_is_kept_with_null_scores_and_correct_local_day() -> None:
    result = normalize_whoop(
        "sleep",
        {
            "id": "sleep-1",
            "user_id": 10129,
            "start": "2026-09-03T22:30:00Z",
            "end": "2026-09-04T06:30:00Z",
            "timezone_offset": "+03:00",
            "score_state": "PENDING_SCORE",
            "nap": False,
        },
    )

    assert result.values["local_day"] == date(2026, 9, 4)
    assert result.values["sleep_performance_percentage"] is None
    assert result.values["total_sleep_milli"] is None


def test_sleep_stage_totals_produce_total_sleep() -> None:
    result = normalize_whoop(
        "sleep",
        {
            "id": "sleep-1",
            "user_id": 10129,
            "start": "2026-09-03T22:30:00Z",
            "score": {
                "stage_summary": {
                    "total_light_sleep_time_milli": 10,
                    "total_slow_wave_sleep_time_milli": 20,
                    "total_rem_sleep_time_milli": 30,
                }
            },
        },
    )

    assert result.values["total_sleep_milli"] == 60


def test_body_is_a_current_snapshot_not_a_dated_measurement() -> None:
    result = normalize_whoop(
        "body",
        {"height_meter": 1.82, "weight_kilogram": 80.5, "max_heart_rate": 190},
    )

    assert result.external_id == "current"
    assert result.source_updated_at is None
    assert result.values["weight_kilogram"] == Decimal("80.5")


def test_fractional_recovery_value_and_full_source_are_preserved() -> None:
    payload = {
        "cycle_id": 93845,
        "user_id": 10129,
        "updated_at": "2026-09-04T08:00:00Z",
        "score": {"resting_heart_rate": 63.75},
        "created_at": "2026-09-04T07:00:00Z",
        "future_official_field": {"value": 1},
    }

    result = normalize_whoop("recovery", payload)

    assert result.values["resting_heart_rate"] == Decimal("63.75")
    assert result.values["source_values"] == payload


def test_missing_identity_is_rejected() -> None:
    with pytest.raises(WhoopNormalizationError, match="id"):
        normalize_whoop("workout", {"start": "2026-09-01T00:00:00Z"})


@pytest.mark.parametrize(
    ("resource_kind", "payload"),
    (
        ("profile", {"user_id": "not-an-integer"}),
        ("recovery", {"cycle_id": "not-an-integer", "user_id": 10129}),
    ),
)
def test_invalid_numeric_identity_is_safe_normalization_error(
    resource_kind: str, payload: dict[str, object]
) -> None:
    with pytest.raises(WhoopNormalizationError, match="invalid"):
        normalize_whoop(resource_kind, payload)
