"""Hourly outdoor model context, kept distinct from bedroom observations."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from health_agent.automation.storage import atomic_private_write
from health_agent.config import Settings
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.storage import PilotRecord, PilotStore
from health_agent.research.calendar import STUDY_START, ZONE, bounds

URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
UNITS = {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "dew_point_2m": "°C",
    "pressure_msl": "hPa",
    "surface_pressure": "hPa",
    "wind_speed_10m": "m/s",
    "wind_direction_10m": "°",
    "wind_gusts_10m": "m/s",
    "precipitation": "mm",
    "cloud_cover": "%",
}


def location(settings: Settings) -> dict[str, Any] | None:
    path = settings.research_root / "weather-location.json"
    if not path.exists():
        return None
    config = json.loads(path.read_text())
    for field, limit in [("latitude", 90), ("longitude", 180)]:
        value = config.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or abs(value) > limit
        ):
            raise ValueError("invalid_weather_location")
    if not isinstance(config.get("label"), str) or not config["label"].strip():
        raise ValueError("missing_weather_location_label")
    config["key"] = hashlib.sha256(
        f"{config['latitude']},{config['longitude']}".encode()
    ).hexdigest()[:16]
    return config


def normalize(
    raw: dict[str, Any], start: datetime, end: datetime
) -> list[dict[str, Any]]:
    """Validate the whole response before writing any normalized points."""
    if raw.get("utc_offset_seconds") != 0:
        raise ValueError("weather_not_utc")
    series, units = raw["hourly"], raw["hourly_units"]
    times = series["time"]
    for field, unit in UNITS.items():
        if units.get(field) != unit or len(series[field]) != len(times):
            raise ValueError("weather_schema_or_units_changed")
    rows = []
    seen = set()
    for index, timestamp in enumerate(times):
        at = datetime.fromisoformat(timestamp)
        at = at.replace(tzinfo=UTC) if at.tzinfo is None else at.astimezone(UTC)
        if at.minute or at.second or at.microsecond or at in seen:
            raise ValueError("invalid_weather_hour")
        seen.add(at)
        values = {}
        for field in UNITS:
            value = series[field][index]
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("invalid_weather_value")
            values[field] = value
        if start <= at < end:
            rows.append({"at": at.isoformat(), "values": values})
    return rows


def sync(
    settings: Settings, engine: Engine, now: datetime, *, force: bool = False
) -> dict[str, Any]:
    config = location(settings)
    if config is None:
        return {"status": "not_configured"}
    root = settings.research_root / "weather"
    status_path = root / "status.json"
    today = now.astimezone(ZONE).date()
    if not force and status_path.exists():
        cached = json.loads(status_path.read_text())
        if (
            cached.get("synced_local_day") == today.isoformat()
            and cached.get("status") == "ok"
        ):
            return cached
    # Replay the whole small study initially, then the recent week for source revisions.
    first = (
        max(STUDY_START, today - timedelta(days=7))
        if status_path.exists()
        else STUDY_START
    )
    start, end = bounds(first)[0], bounds(today)[0]
    store = PilotStore(engine)
    try:
        if end <= start:
            return {"status": "waiting_for_completed_day"}
        params = {
            "latitude": config["latitude"],
            "longitude": config["longitude"],
            "start_date": start.date().isoformat(),
            "end_date": (today - timedelta(days=1)).isoformat(),
            "hourly": ",".join(UNITS),
            "timezone": "GMT",
            "wind_speed_unit": "ms",
        }
        response = httpx.get(URL, params=params, timeout=30)
        response.raise_for_status()
        raw = response.json()
        points = normalize(raw, start, end)
        body = json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
        digest = hashlib.sha256(body).hexdigest()
        atomic_private_write(root / "raw" / f"{digest}.json", body)
        store.put(
            DEFAULT_PROFILE_ID,
            "shared",
            "weather_raw",
            digest,
            {
                "source": URL,
                "retrieved_at": now.isoformat(),
                "request": params,
                "location": config,
                "sha256": digest,
                "raw": raw,
            },
            at=now,
        )
        for point in points:
            key = config["key"] + ":" + point["at"]
            payload = {
                **point,
                "source": URL,
                "source_kind": "historical_forecast_model",
                "location": config,
                "units": UNITS,
                "raw_sha256": digest,
                "retrieved_at": now.isoformat(),
            }
            prior = store.by_source(DEFAULT_PROFILE_ID, "shared", "weather_hour", key)
            if prior is None:
                store.put(
                    DEFAULT_PROFILE_ID,
                    "shared",
                    "weather_hour",
                    key,
                    payload,
                    at=datetime.fromisoformat(point["at"]),
                )
            else:
                store.patch(DEFAULT_PROFILE_ID, prior.id, payload)
        daily = []
        day = first
        while day < today:
            day_rows = [
                p
                for p in points
                if datetime.fromisoformat(p["at"]).astimezone(ZONE).date() == day
            ]
            missing = {
                field: sum(p["values"][field] is None for p in day_rows)
                for field in UNITS
            }
            daily.append(
                {
                    "date": day.isoformat(),
                    "hours": len(day_rows),
                    "missing_fields": missing,
                    "status": "ok"
                    if len(day_rows) == 24 and not any(missing.values())
                    else "attention",
                }
            )
            day += timedelta(days=1)
        result = {
            "status": "ok"
            if daily and all(d["status"] == "ok" for d in daily)
            else "attention",
            "checked_at": now.isoformat(),
            "synced_local_day": today.isoformat(),
            "source_kind": "historical_forecast_model",
            "hourly_rows": len(points),
            "daily": daily,
        }
    except Exception:  # noqa: BLE001 - isolate provider failures, preserve previous observations
        result = {
            "status": "attention",
            "checked_at": now.isoformat(),
            "safe_error": "weather_sync_failed",
        }
    atomic_private_write(status_path, json.dumps(result, indent=2).encode())
    return result


def export_day(settings: Settings, engine: Engine, day: date) -> dict[str, Any]:
    config = location(settings)
    if config is None:
        return {"status": "not_configured"}
    start, end = bounds(day)
    with Session(engine) as session:
        records = list(
            session.scalars(
                select(PilotRecord)
                .where(
                    PilotRecord.profile_id == DEFAULT_PROFILE_ID,
                    PilotRecord.domain == "shared",
                    PilotRecord.kind == "weather_hour",
                    PilotRecord.at >= start,
                    PilotRecord.at < end,
                    PilotRecord.source_key.startswith(config["key"] + ":"),
                )
                .order_by(PilotRecord.at)
            )
        )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["at_utc", *UNITS, "source_kind", "retrieved_at", "raw_sha256"])
    for record in records:
        p = record.payload
        writer.writerow(
            [
                record.at.isoformat(),
                *[p["values"].get(k) for k in UNITS],
                p["source_kind"],
                p["retrieved_at"],
                p["raw_sha256"],
            ]
        )
    path = settings.research_root / "datasets" / day.isoformat() / "weather-hourly.csv"
    data = output.getvalue().encode()
    atomic_private_write(path, data)
    return {
        "status": "exported",
        "hours": len(records),
        "source": URL,
        "source_kind": "historical_forecast_model",
        "units": UNITS,
        "filename": path.name,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
    }
