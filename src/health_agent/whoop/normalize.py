from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


class WhoopNormalizationError(ValueError):
    """The upstream object lacks the identity or timestamps required to store it."""


@dataclass(frozen=True, slots=True)
class NormalizedWhoopRecord:
    resource_kind: str
    external_id: str
    payload_hash: str
    source_updated_at: datetime | None
    values: dict[str, Any]


def canonical_payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def normalize_whoop(
    resource_kind: str, payload: dict[str, Any]
) -> NormalizedWhoopRecord:
    normalizers = {
        "profile": _normalize_profile,
        "body": _normalize_body,
        "cycle": _normalize_cycle,
        "recovery": _normalize_recovery,
        "sleep": _normalize_sleep,
        "workout": _normalize_workout,
    }
    try:
        normalizer = normalizers[resource_kind]
    except KeyError as error:
        raise WhoopNormalizationError("Unsupported WHOOP resource kind") from error
    external_id, updated_at, values = normalizer(payload)
    return NormalizedWhoopRecord(
        resource_kind=resource_kind,
        external_id=external_id,
        payload_hash=canonical_payload_hash(payload),
        source_updated_at=updated_at,
        values=values,
    )


def _normalize_profile(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    user_id = _required_id(payload, "user_id")
    return (
        user_id,
        None,
        {
            "external_user_id": int(user_id),
            "email": _optional_string(payload, "email"),
            "first_name": _optional_string(payload, "first_name"),
            "last_name": _optional_string(payload, "last_name"),
        },
    )


def _normalize_body(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    return (
        "current",
        None,
        {
            "height_meter": _decimal(payload.get("height_meter")),
            "weight_kilogram": _decimal(payload.get("weight_kilogram")),
            "max_heart_rate": _integer(payload.get("max_heart_rate")),
        },
    )


def _normalize_cycle(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    external_id = _required_id(payload, "id")
    start = _datetime(payload.get("start"), required=True)
    score = _score(payload)
    return (
        external_id,
        _datetime(payload.get("updated_at")),
        {
            "external_user_id": _integer(payload.get("user_id")),
            "start_at": start,
            "end_at": _datetime(payload.get("end")),
            "local_day": _local_day(start, payload.get("timezone_offset")),
            "timezone_offset": _optional_string(payload, "timezone_offset"),
            "score_state": _optional_string(payload, "score_state"),
            "strain": _decimal(score.get("strain")),
            "kilojoule": _decimal(score.get("kilojoule")),
            "average_heart_rate": _integer(score.get("average_heart_rate")),
            "max_heart_rate": _integer(score.get("max_heart_rate")),
        },
    )


def _normalize_recovery(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    external_id = _required_id(payload, "cycle_id")
    score = _score(payload)
    return (
        external_id,
        _datetime(payload.get("updated_at")),
        {
            "cycle_id": int(external_id),
            "sleep_id": _optional_string(payload, "sleep_id"),
            "external_user_id": _integer(payload.get("user_id")),
            "score_state": _optional_string(payload, "score_state"),
            "user_calibrating": _boolean(score.get("user_calibrating")),
            "recovery_score": _decimal(score.get("recovery_score")),
            "resting_heart_rate": _integer(score.get("resting_heart_rate")),
            "hrv_rmssd_milli": _decimal(score.get("hrv_rmssd_milli")),
            "spo2_percentage": _decimal(score.get("spo2_percentage")),
            "skin_temp_celsius": _decimal(score.get("skin_temp_celsius")),
        },
    )


def _normalize_sleep(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    external_id = _required_id(payload, "id")
    start = _datetime(payload.get("start"), required=True)
    score = _score(payload)
    stages = _nested(score, "stage_summary")
    needed = _nested(score, "sleep_needed")
    total_sleep = sum(
        value or 0
        for value in (
            _integer(stages.get("total_light_sleep_time_milli")),
            _integer(stages.get("total_slow_wave_sleep_time_milli")),
            _integer(stages.get("total_rem_sleep_time_milli")),
        )
    )
    return (
        external_id,
        _datetime(payload.get("updated_at")),
        {
            "cycle_id": _integer(payload.get("cycle_id")),
            "external_user_id": _integer(payload.get("user_id")),
            "start_at": start,
            "end_at": _datetime(payload.get("end")),
            "local_day": _local_day(start, payload.get("timezone_offset")),
            "timezone_offset": _optional_string(payload, "timezone_offset"),
            "is_nap": _boolean(payload.get("nap")),
            "score_state": _optional_string(payload, "score_state"),
            "sleep_performance_percentage": _decimal(
                score.get("sleep_performance_percentage")
            ),
            "sleep_consistency_percentage": _decimal(
                score.get("sleep_consistency_percentage")
            ),
            "sleep_efficiency_percentage": _decimal(
                score.get("sleep_efficiency_percentage")
            ),
            "respiratory_rate": _decimal(score.get("respiratory_rate")),
            "total_in_bed_milli": _integer(stages.get("total_in_bed_time_milli")),
            "total_awake_milli": _integer(stages.get("total_awake_time_milli")),
            "total_no_data_milli": _integer(stages.get("total_no_data_time_milli")),
            "total_light_sleep_milli": _integer(
                stages.get("total_light_sleep_time_milli")
            ),
            "total_slow_wave_sleep_milli": _integer(
                stages.get("total_slow_wave_sleep_time_milli")
            ),
            "total_rem_sleep_milli": _integer(stages.get("total_rem_sleep_time_milli")),
            "total_sleep_milli": total_sleep if stages else None,
            "sleep_cycle_count": _integer(stages.get("sleep_cycle_count")),
            "disturbance_count": _integer(stages.get("disturbance_count")),
            "baseline_needed_milli": _integer(needed.get("baseline_milli")),
            "sleep_debt_needed_milli": _integer(
                needed.get("need_from_sleep_debt_milli")
            ),
            "strain_needed_milli": _integer(
                needed.get("need_from_recent_strain_milli")
            ),
            "nap_credit_milli": _integer(needed.get("need_from_recent_nap_milli")),
        },
    )


def _normalize_workout(
    payload: dict[str, Any],
) -> tuple[str, datetime | None, dict[str, Any]]:
    external_id = _required_id(payload, "id")
    start = _datetime(payload.get("start"), required=True)
    score = _score(payload)
    zones = _nested(score, "zone_durations")
    return (
        external_id,
        _datetime(payload.get("updated_at")),
        {
            "external_user_id": _integer(payload.get("user_id")),
            "start_at": start,
            "end_at": _datetime(payload.get("end")),
            "local_day": _local_day(start, payload.get("timezone_offset")),
            "timezone_offset": _optional_string(payload, "timezone_offset"),
            "sport_id": _integer(payload.get("sport_id")),
            "sport_name": _optional_string(payload, "sport_name"),
            "score_state": _optional_string(payload, "score_state"),
            "strain": _decimal(score.get("strain")),
            "average_heart_rate": _integer(score.get("average_heart_rate")),
            "max_heart_rate": _integer(score.get("max_heart_rate")),
            "kilojoule": _decimal(score.get("kilojoule")),
            "percent_recorded": _decimal(score.get("percent_recorded")),
            "distance_meter": _decimal(score.get("distance_meter")),
            "altitude_gain_meter": _decimal(score.get("altitude_gain_meter")),
            "altitude_change_meter": _decimal(score.get("altitude_change_meter")),
            "zone_zero_milli": _integer(zones.get("zone_zero_milli")),
            "zone_one_milli": _integer(zones.get("zone_one_milli")),
            "zone_two_milli": _integer(zones.get("zone_two_milli")),
            "zone_three_milli": _integer(zones.get("zone_three_milli")),
            "zone_four_milli": _integer(zones.get("zone_four_milli")),
            "zone_five_milli": _integer(zones.get("zone_five_milli")),
        },
    )


def _required_id(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)) or str(value) == "":
        raise WhoopNormalizationError(f"WHOOP {key} is missing or invalid")
    return str(value)


def _optional_string(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    return str(value) if value is not None else None


def _datetime(value: Any, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise WhoopNormalizationError("WHOOP timestamp is missing")
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as error:
        raise WhoopNormalizationError("WHOOP timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise WhoopNormalizationError("WHOOP timestamp has no timezone")
    return parsed.astimezone(UTC)


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as error:
        raise WhoopNormalizationError("WHOOP numeric value is invalid") from error


def _integer(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise WhoopNormalizationError("WHOOP integer value is invalid") from error


def _boolean(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _score(payload: dict[str, Any]) -> dict[str, Any]:
    return _nested(payload, "score")


def _nested(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def _local_day(start: datetime | None, raw_offset: Any) -> date | None:
    if start is None:
        return None
    if isinstance(raw_offset, str):
        try:
            sign = -1 if raw_offset.startswith("-") else 1
            hours_text, minutes_text = raw_offset.lstrip("+-").split(":", maxsplit=1)
            offset = timedelta(hours=int(hours_text), minutes=int(minutes_text)) * sign
            return start.astimezone(timezone(offset)).date()
        except (ValueError, OverflowError):
            pass
    return start.date()
