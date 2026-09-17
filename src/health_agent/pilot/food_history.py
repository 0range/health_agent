"""Bounded, factual food records safe to share with the main pilot agent."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Record, Store
from health_agent.pilot.food_carbs import classify

NUTRIENTS = (
    "kcal",
    "protein_g",
    "fat_g",
    "carbs_g",
    "saturated_fat_g",
    "fiber_g",
    "cholesterol_mg",
)

_MOSCOW = ZoneInfo("Europe/Moscow")
_MAX_SELECTED = 100
_MAX_ITEMS = 50
_MAX_TEXT = 200
_MAX_JSON_CHARS = 20_000


def build_food_history(
    store: Store,
    profile_id: UUID,
    now: datetime,
    *,
    days: int = 14,
) -> dict[str, Any]:
    """Project recent meal records without exposing their private source payloads."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now_must_be_timezone_aware")
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 31:
        raise ValueError("days_must_be_between_1_and_31")

    today = now.astimezone(_MOSCOW).date()
    start = today - timedelta(days=days - 1)
    candidates: list[tuple[datetime, Record]] = []
    invalid = 0
    source = store.list(profile_id, "food", "meal", limit=1000)
    for meal in source:
        if meal.payload.get("superseded_by_meal"):
            continue
        occurred = _parse_aware(meal.payload.get("occurred_at"))
        if occurred is None or occurred > now:
            invalid += 1
            continue
        if start <= occurred.astimezone(_MOSCOW).date() <= today:
            candidates.append((occurred, meal))

    candidates.sort(key=lambda item: item[0], reverse=True)
    truncated = len(candidates) > _MAX_SELECTED or len(source) == 1000
    selected = candidates[:_MAX_SELECTED]
    meals = [_project(record) for _, record in selected]
    result: dict[str, Any] = {
        "period": {
            "days": days,
            "from_date": start.isoformat(),
            "to_date": today.isoformat(),
        },
        "recorded_meal_count": len(selected),
        "recorded_days": sorted(
            {occurred.astimezone(_MOSCOW).date().isoformat() for occurred, _ in selected}
        ),
        "truncated": truncated,
        "meals": meals,
        "interpretation_limits": (
            "Only logged meals; missing entries are not fasting. "
            "Nutrients are estimates, unknown is null."
        ),
    }
    if invalid:
        result["invalid_record_count"] = invalid
    while result["meals"] and len(json.dumps(result, ensure_ascii=False)) > _MAX_JSON_CHARS:
        result["meals"].pop()
        result["truncated"] = True
        result["recorded_meal_count"] = len(result["meals"])
        retained = selected[: len(result["meals"])]
        result["recorded_days"] = sorted(
            {occurred.astimezone(_MOSCOW).date().isoformat() for occurred, _ in retained}
        )
    return result


def _project(meal: Record) -> dict[str, Any]:
    raw_analysis = meal.payload.get("analysis")
    analysis = raw_analysis if isinstance(raw_analysis, dict) else {}
    projected: dict[str, Any] = {
        "recorded_at": meal.payload["occurred_at"],
        "category": _bounded_optional(meal.payload.get("category")),
        "foods": _bounded_list(analysis.get("foods")),
        "portion_estimate": _bounded_optional(analysis.get("portion_estimate")),
        "nutrients_estimated": {
            key: _nonnegative_number(analysis.get(key)) for key in NUTRIENTS
        },
        "analysis_available": bool(analysis),
        "carbohydrate_sources": classify(analysis.get("foods")),
        "unknowns": _bounded_list(analysis.get("unknowns")),
    }
    user_kcal = meal.payload.get("user_kcal")
    if isinstance(user_kcal, dict) and _nonnegative_number(user_kcal.get("value")) is not None:
        projected["nutrients_estimated"]["kcal"] = _nonnegative_number(user_kcal["value"])
        projected["kcal_source"] = "user_estimate"
        projected["model_kcal_estimate"] = _nonnegative_number(analysis.get("kcal"))
    ended_at = meal.payload.get("ended_at")
    end_source = meal.payload.get("end_source")
    if (
        _parse_aware(ended_at) is not None
        and end_source == "last_photo_plus_20m"
    ):
        projected["ended_at"] = ended_at
        projected["end_source"] = end_source
    return projected


def _parse_aware(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _bounded_optional(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:_MAX_TEXT]


def _bounded_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip()[:_MAX_TEXT] for item in value if isinstance(item, str) and item.strip()][
        :_MAX_ITEMS
    ]


def _nonnegative_number(value: Any) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
    ):
        return None
    return value
