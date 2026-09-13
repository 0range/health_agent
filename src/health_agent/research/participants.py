"""Combine explicit participants' checks without hiding one person's outage."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Engine

from health_agent.automation.storage import atomic_private_write
from health_agent.config import Settings
from health_agent.qingping.service import Connection
from health_agent.research.calendar import ZONE, recent_days
from health_agent.research.dataset import export_day
from health_agent.research.quality import QualityService
from health_agent.research.weather import sync as sync_weather
from health_agent.whoop.participants import targets


def check_participants(
    settings: Settings, engine: Engine, sensor: Connection, now: datetime | None = None
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    enrolled = targets(settings)
    profiles = [t.profile_id or sensor.profile_id for t in enrolled]
    if len(set(profiles)) != len(profiles):
        raise ValueError("duplicate_whoop_participant_profile")
    results = []
    for target, profile_id in zip(enrolled, profiles, strict=True):
        try:
            result = QualityService(
                engine,
                sensor,
                settings.research_root,
                target.root,
                settings.qingping_root,
                settings.research_postgres_container,
                profile_id=profile_id,
            ).run(now, persist_status=False)
        except Exception:  # noqa: BLE001 - isolate failures and redact private upstream errors
            result = {
                "status": "attention",
                "safe_error": "participant_check_failed",
                "checked_at": now.isoformat(),
                "daily_reports": [],
                "configured_whoops": 0,
                "sources": {},
            }
        result["profile_id"] = str(profile_id)
        atomic_private_write(
            settings.research_root / "participants" / str(profile_id) / "status.json",
            json.dumps(result, indent=2).encode(),
        )
        results.append(result)
    combined = {
        **results[0],
        "status": "ok" if all(r["status"] == "ok" for r in results) else "attention",
        "configured_whoops": sum(r["configured_whoops"] for r in results),
        "enrolled_participants": len(results),
        "daily_reports": [d for r in results for d in r["daily_reports"]],
        "participants": results,
    }
    combined.pop("profile_id", None)
    combined["sources"] = {
        "air": results[0]["sources"].get("air", {"status": "unknown"})
    }
    for name in ("whoop_detail", "whoop_public"):
        per_person = {
            r["profile_id"]: r["sources"].get(name, {"status": "unknown"})
            for r in results
        }
        combined["sources"][name] = {
            "status": "ok"
            if all(s["status"] == "ok" for s in per_person.values())
            else "attention",
            "participants": per_person,
        }
    try:
        weather = sync_weather(settings, engine, now) if now.astimezone(ZONE).hour >= 10 else {"status": "scheduled_after_10"}
    except Exception:  # noqa: BLE001 - invalid optional config cannot block core collection
        weather = {"status": "attention", "safe_error": "weather_config_failed"}
    combined["sources"]["weather"] = weather
    if weather["status"] == "attention":
        combined["status"] = "attention"
    combined["datasets"] = []
    for day in recent_days(now) if now.astimezone(ZONE).hour >= 10 else []:
        try:
            manifest = export_day(settings, engine, sensor, day, now)
            combined["datasets"].append(
                {
                    "date": day.isoformat(),
                    "status": "exported",
                    "rows": manifest["rows"],
                }
            )
        except Exception:  # noqa: BLE001 - surface a safe export failure without private source data
            combined["status"] = "attention"
            combined["datasets"].append({"date": day.isoformat(), "status": "failed"})
    atomic_private_write(
        settings.research_root / "status.json", json.dumps(combined, indent=2).encode()
    )
    return combined
