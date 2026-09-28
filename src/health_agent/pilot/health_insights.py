"""Personal observations across food, measured weight, WHOOP and COROS."""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Store
from health_agent.pilot.food_assessment import LABELS
from health_agent.pilot.sleep_context import night_contexts

ZONE = ZoneInfo("Europe/Moscow")


def number(value: Any, maximum: float = 1_000_000) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        return None
    return float(value) if math.isfinite(value) and 0 <= value <= maximum else None


def timestamp(value: Any) -> datetime | None:
    try:
        result = datetime.fromisoformat(str(value))
        return result if result.tzinfo is not None else None
    except (ValueError, TypeError):
        return None


def period(now: datetime) -> tuple[date, date]:
    if now.tzinfo is None:
        raise ValueError("now_requires_timezone")
    last = now.astimezone(ZONE).date() - timedelta(days=1)
    return last - timedelta(days=27), last


def _comparison(rows: list[dict[str, Any]], key: str, minimum: int) -> dict[str, Any]:
    recent = [r[key] for r in rows[-7:] if r.get(key) is not None]
    previous = [r[key] for r in rows[-14:-7] if r.get(key) is not None]
    good = len(recent) >= minimum and len(previous) >= minimum
    return {"recent": round(mean(recent), 2) if recent else None,
            "previous": round(mean(previous), 2) if previous else None,
            "recent_days": len(recent), "previous_days": len(previous),
            "delta": round(mean(recent) - mean(previous), 2) if good else None,
            "enough_for_comparison": good}


def build_insights(
    store: Store, profile: UUID, now: datetime, whoop: dict[str, Any],
) -> dict[str, Any]:
    first, last = period(now)
    days: dict[str, dict[str, Any]] = {}
    for offset in range(28):
        day_key = (first + timedelta(days=offset)).isoformat()
        days[day_key] = {"date": day_key, "food_entries": 0, "main_slots": [], "snacks": 0,
                     "kcal_recorded": 0.0, "kcal_missing": 0, "protein_recorded_g": 0.0,
                     "protein_missing": 0, "training_count": 0, "training_minutes": 0.0,
                     "duration_missing": 0, "coros_day_checked": False, "weight_kg": None,
                     "sleep_hours": None, "hrv_ms": None, "resting_hr": None, "recovery": None}
    truncated = False

    def rows(domain: str, kind: str):
        nonlocal truncated
        result = store.list(profile, domain, kind, limit=1000)
        if len(result) == 1000 and result[-1].at.astimezone(ZONE).date() >= first:
            truncated = True
        return result

    for row in rows("food", "meal"):
        p = row.payload
        at = timestamp(p.get("occurred_at"))
        if p.get("superseded_by_meal") or at is None or at > now:
            continue
        entry = days.get(at.astimezone(ZONE).date().isoformat())
        if entry is None:
            continue
        entry["food_entries"] += 1
        category = p.get("category")
        if category in LABELS and category not in entry["main_slots"]:
            entry["main_slots"].append(category)
        if category == "bridge_snack":
            entry["snacks"] += 1
        analysis = p.get("analysis") or {}
        kcal = number((p.get("user_kcal") or {}).get("value", analysis.get("kcal")))
        protein = number(analysis.get("protein_g"))
        for value, total, missing in ((kcal, "kcal_recorded", "kcal_missing"), (protein, "protein_recorded_g", "protein_missing")):
            if value is None:
                entry[missing] += 1
            else:
                entry[total] += value
        if category == "dinner" and p.get("time_source") == "user":
            previous = timestamp(entry.get("dinner_at"))
            if previous is None or previous < at:
                entry["dinner_at"] = at.isoformat()

    weighed: dict[str, set[tuple[str, float]]] = defaultdict(set)
    latest_weight: dict[str, Any] | None = None
    for row in rows("shared", "weight"):
        kg = number(row.payload.get("weight_kg"), 500)
        if not kg or row.at > now:
            continue
        day = row.at.astimezone(ZONE).date().isoformat()
        if latest_weight is None or row.at > datetime.fromisoformat(latest_weight["at"]):
            latest_weight = {"kg": kg, "date": day, "at": row.at.isoformat(), "source": row.payload.get("source"),
                             "timestamp_precision": row.payload.get("timestamp_precision", "instant")}
        if day in days:
            weighed[day].add((row.at.isoformat(), kg))
    for day, values in weighed.items():
        days[day]["weight_kg"] = float(median(v for _, v in values))

    composition_fields = {"body_fat_percent": 100, "muscle_mass_kg": 500, "muscle_percent": 100}
    composition_days: dict[str, dict[str, set[float]]] = defaultdict(lambda: defaultdict(set))
    latest_body: dict[str, Any] | None = None
    for row in rows("shared", "body_measurement"):
        p = row.payload
        recorded_at = timestamp(p.get("recorded_at"))
        if row.at > now or recorded_at is None or recorded_at > now:
            continue
        day = row.at.astimezone(ZONE).date().isoformat()
        if p.get("measurement_date") != day:
            continue
        measurements = {k: number(p.get(k), limit) for k, limit in composition_fields.items()}
        if not any(v is not None for v in measurements.values()):
            continue
        if (latest_body is None or (day, recorded_at.isoformat()) >
                (latest_body["date"], latest_body["recorded_at"])):
            latest_body = {**measurements, "date": day, "recorded_at": recorded_at.isoformat(),
                           "source": p.get("source"), "timestamp_precision": "day"}
        if day in days:
            for field, value in measurements.items():
                if value is not None:
                    composition_days[day][field].add(value)
    for day, fields in composition_days.items():
        for field, composition_values in fields.items():
            days[day][field] = float(median(composition_values))

    activities: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows("training", "activity"):
        identifier = str(row.payload.get("id") or row.source_key)
        if identifier in seen or row.at > now:
            continue
        seen.add(identifier)
        entry = days.get(row.at.astimezone(ZONE).date().isoformat())
        if entry is None:
            continue
        entry["training_count"] += 1
        duration = number(row.payload.get("duration_s"), 172_800)
        if duration is None:
            entry["duration_missing"] += 1
        else:
            entry["training_minutes"] += duration / 60
        activities.append({"at": row.at.isoformat(), "day": entry["date"],
                           "precision": row.payload.get("timestamp_precision", "instant"),
                           "duration_s": duration})
    runs = rows("training", "sync_run")
    for run in runs:
        p = run.payload
        if p.get("status") != "success" or run.at > now:
            continue
        finished = timestamp(p.get("finished_at")) or run.at
        for entry in days.values():
            day_end = datetime.combine(date.fromisoformat(entry["date"]) + timedelta(days=1), time.min, ZONE)
            if str(p.get("since", "9999")) <= entry["date"] <= str(p.get("until", "0000")) and day_end <= finished <= now:
                entry["coros_day_checked"] = True

    manual_days: set[str] = set()
    for row in rows("training", "manual_activity"):
        day = row.payload.get("date")
        if row.at <= now and day in days:
            manual_days.add(day)
    for day in manual_days:
        days[day]["manual_workout_reported"] = True

    for row in rows("sleep", "diary"):
        checkin = row.payload.get("checkin") or {}
        entry = days.get(str(checkin.get("wake_date")))
        rested = number(checkin.get("rested_0_10"), 10)
        if entry is not None and rested is not None and row.at <= now:
            entry.setdefault("rested", rested)

    sleeps: dict[str, dict[str, Any]] = {}
    for sleep in whoop.get("records", []):
        start, end = timestamp(sleep.get("start_at")), timestamp(sleep.get("end_at"))
        hours = number(sleep.get("sleep_hours"), 24)
        if start is None or end is None or not start < end <= now or hours is None:
            continue
        day = end.astimezone(ZONE).date().isoformat()
        if day not in days or (day in sleeps and sleeps[day]["sleep_hours"] >= hours):
            continue
        sleeps[day] = {**sleep, "sleep_hours": hours}
    for day, sleep in sleeps.items():
        for key, limit in (("sleep_hours", 24), ("hrv_ms", 1000), ("resting_hr", 250), ("recovery", 100)):
            days[day][key] = number(sleep.get(key), limit)

    locations = {c["wake_date"]: c for c in night_contexts(store, profile, first, last)
                 if (timestamp(c["recorded_at"]) or now) <= now}
    for day, context in locations.items():
        days[day]["location"] = context["location"]
    daily = list(days.values())
    for entry in daily:
        entry["kcal_four_slots"] = round(entry["kcal_recorded"]) if len(entry["main_slots"]) == 4 and not entry["kcal_missing"] else None
        entry["protein_four_slots"] = round(entry["protein_recorded_g"], 1) if len(entry["main_slots"]) == 4 and not entry["protein_missing"] else None
        for key in ("kcal_recorded", "protein_recorded_g", "training_minutes"):
            entry[key] = round(entry[key], 1)
    recent = daily[-7:]
    weight = _comparison(daily, "weight_kg", 2)
    weight["delta_kg"] = weight["delta"]
    latest_sync = next((r for r in runs if r.at <= now), None)
    result: dict[str, Any] = {
        "as_of": now.isoformat(), "first_date": first.isoformat(), "through_date": last.isoformat(),
        "recent_from": (last - timedelta(days=6)).isoformat(), "daily": daily,
        "weight": {**weight, "latest": latest_weight},
        "body_composition": {"latest": latest_body, "method": "reported_scale_estimates",
                             **{k: _comparison(daily, k, 2) for k in composition_fields}},
        "food": {"entries": sum(d["food_entries"] for d in recent),
                 "days": sum(d["food_entries"] > 0 for d in recent),
                 "four_slot_days": sum(len(d["main_slots"]) == 4 for d in recent),
                 "unknown_calories": sum(d["kcal_missing"] for d in recent),
                 "calories": _comparison(daily, "kcal_four_slots", 3),
                 "protein": _comparison(daily, "protein_four_slots", 3)},
        "training": {"sessions": sum(d["training_count"] for d in recent),
                     "days": sum(d["training_count"] > 0 for d in recent),
                     "minutes": round(sum(d["training_minutes"] for d in recent)),
                     "previous_sessions": sum(d["training_count"] for d in daily[-14:-7]),
                     "previous_days": sum(d["training_count"] > 0 for d in daily[-14:-7]),
                     "previous_minutes": round(sum(d["training_minutes"] for d in daily[-14:-7])),
                     "duration_missing": sum(d["duration_missing"] for d in recent),
                     "checked_days": sum(d["coros_day_checked"] for d in recent),
                     "manual_days_without_coros": sum(d.get("manual_workout_reported", False) and not d["training_count"] for d in recent),
                     "sync_status": latest_sync.payload.get("status") if latest_sync else None,
                     "sync_at": (latest_sync.payload.get("finished_at") or latest_sync.at.isoformat()) if latest_sync else None},
        "whoop": {k: v for k, v in whoop.items() if k != "records"},
        "recovery": {k: _comparison(daily, k, 4) for k in ("sleep_hours", "hrv_ms", "resting_hr", "recovery", "rested")},
        "associations": _associations(days, sleeps, activities, locations),
        "truncated": truncated or whoop.get("truncated", False),
        "limits": ["logged food is not a complete diet", "weight change is not measured fat loss",
                   "body composition is estimated by the scale; kg and percent are separate",
                   "day precision timestamps are storage anchors, not known weighing times",
                   "COROS absence is not proof of inactivity", "wearable estimates are not diagnoses",
                   "observational comparisons cannot establish causality"],
    }
    if result["truncated"]:
        for association in result["associations"]:
            association.update(status="insufficient", difference_hours=None, reason="truncated_inputs")
    return result


def _associations(days, sleeps, activities, locations) -> list[dict[str, Any]]:
    groups: dict[str, tuple[list[float], list[float]]] = {"dinner_gap": ([], []), "training": ([], [])}
    away = 0
    for day, sleep in sleeps.items():
        if locations.get(day, {}).get("location") == "away":
            away += 1
            continue
        start = datetime.fromisoformat(sleep["start_at"])
        prior = start - timedelta(hours=18)
        previous_days = {prior.astimezone(ZONE).date().isoformat(), start.astimezone(ZONE).date().isoformat()}
        candidates = [timestamp(days[d].get("dinner_at")) for d in previous_days if d in days]
        dinners = [d for d in candidates if d is not None and timedelta(0) <= start - d <= timedelta(hours=8)]
        if dinners:
            gap = (start - max(dinners)).total_seconds() / 3600
            groups["dinner_gap"][0 if gap < 2 else 1].append(sleep["sleep_hours"])
        # A fixed preceding 18 h window avoids attributing exercise after sleep to that night.
        exact = [a for a in activities if a["precision"] != "day" and prior <= datetime.fromisoformat(a["at"]) < start
                 and a["duration_s"] is not None and datetime.fromisoformat(a["at"]) + timedelta(seconds=a["duration_s"]) <= start]
        uncertain = any(a["precision"] == "day" and a["day"] in previous_days for a in activities)
        checked = all(d in days and days[d]["coros_day_checked"] for d in previous_days)
        manual = any(days.get(d, {}).get("manual_workout_reported") for d in previous_days)
        if exact and not uncertain:
            groups["training"][0].append(sleep["sleep_hours"])
        elif checked and not uncertain and not manual and not any(a["day"] in previous_days for a in activities):
            groups["training"][1].append(sleep["sleep_hours"])
    result = []
    for name, (left, right) in groups.items():
        enough = min(len(left), len(right)) >= 4 and len(left) + len(right) >= 10
        result.append({"question": name, "status": "exploratory" if enough else "insufficient",
                       "left_nights": len(left), "right_nights": len(right), "away_excluded": away,
                       "difference_hours": round(mean(left) - mean(right), 2) if enough else None,
                       "interpretation": "association_only_not_causal"})
    return result
