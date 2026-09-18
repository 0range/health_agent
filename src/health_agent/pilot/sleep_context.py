"""User-reported night location, distinct from measurements and full-day presence."""

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Store

ZONE = ZoneInfo("Europe/Moscow")


def night_contexts(
    store: Store, profile: UUID, first: date, last: date,
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for record in store.list(profile, "shared", "sleep_context", limit=1000):
        payload = record.payload
        try:
            wake = date.fromisoformat(payload["wake_date"])
            start = date.fromisoformat(payload["night_start_date"])
        except (KeyError, TypeError, ValueError):
            continue
        if (not first <= wake <= last or start != wake - timedelta(days=1)
                or payload.get("source") != "user"
                or payload.get("timezone") != str(ZONE)
                or payload.get("evidence") not in {"reported", "planned"}
                or payload.get("location") not in {"away", "home", "unknown"}
                or payload.get("status", "active") != "active"):
            continue
        key = wake.isoformat()
        if key not in selected:
            selected[key] = {
                "night_start_date": start.isoformat(), "wake_date": key,
                "recorded_at": record.at.isoformat(), "source": "user",
                "timezone": str(ZONE), "location": payload["location"],
                "location_label": payload.get("location_label"),
                "reason": payload.get("reason"), "evidence": payload["evidence"],
                "room_comparison": "exclude_away" if payload["location"] == "away" else "unknown",
            }
    return [selected[key] for key in sorted(selected)]


def sleep_comparisons(
    sleeps: list[dict[str, Any]], contexts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_date = {note["wake_date"]: note for note in contexts}
    result = []
    for sleep in sleeps:
        end = datetime.fromisoformat(sleep["end_at"])
        if end.tzinfo is None:
            raise ValueError("sleep_end_requires_timezone")
        wake = end.astimezone(ZONE).date().isoformat()
        # A report about the night does not establish location of a daytime nap.
        note = by_date.get(wake) if sleep.get("nap") is False else None
        result.append({
            "sleep_id": sleep["sleep_id"], "wake_date": wake,
            "room_comparison": note["room_comparison"] if note else "unknown",
            "evidence": note["evidence"] if note else None,
            "location": note["location"] if note else "unknown",
        })
    return result
