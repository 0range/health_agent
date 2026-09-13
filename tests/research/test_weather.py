import copy
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.pilot.storage import PilotRecord
from health_agent.research.calendar import bounds
from health_agent.research.weather import UNITS, export_day, normalize, sync


def response(start, hours=24):
    return {
        "utc_offset_seconds": 0,
        "hourly_units": UNITS,
        "hourly": {
            "time": [
                (start + timedelta(hours=i)).replace(tzinfo=None).isoformat()
                for i in range(hours)
            ],
            **{key: [10.0] * hours for key in UNITS},
        },
    }


def test_normalize_excludes_future_preserves_null_and_rejects_units():
    start, end = bounds(date(2026, 9, 12))
    raw = response(start, 26)
    raw["hourly"]["temperature_2m"][0] = None
    rows = normalize(raw, start, end)
    assert len(rows) == 24 and rows[0]["values"]["temperature_2m"] is None
    invalid = copy.deepcopy(raw)
    invalid["hourly_units"] = {**UNITS, "wind_speed_10m": "km/h"}
    with pytest.raises(ValueError):
        normalize(invalid, start, end)
    invalid = copy.deepcopy(raw)
    invalid["hourly"]["cloud_cover"].pop()
    with pytest.raises(ValueError):
        normalize(invalid, start, end)
    invalid = copy.deepcopy(raw)
    invalid["hourly"]["time"][1] = invalid["hourly"]["time"][0]
    with pytest.raises(ValueError):
        normalize(invalid, start, end)


def test_weather_backfill_replay_export_and_failure_isolation(
    tmp_path, clean_database, monkeypatch
):
    settings = Settings(research_root=tmp_path)
    (tmp_path / "weather-location.json").write_text(
        json.dumps({"latitude": 55.7, "longitude": 37.5, "label": "synthetic district"})
    )
    start = bounds(date(2026, 9, 10))[0]
    raw = response(start, 72)
    calls = []

    def get(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=raw, request=httpx.Request("GET", url))

    monkeypatch.setattr("health_agent.research.weather.httpx.get", get)
    now = datetime(2026, 9, 13, 8, tzinfo=UTC)
    result = sync(settings, clean_database, now)
    assert result["status"] == "ok" and result["hourly_rows"] == 72
    assert all(d["hours"] == 24 for d in result["daily"])
    sync(settings, clean_database, now)
    assert len(calls) == 1
    sync(settings, clean_database, now, force=True)
    with Session(clean_database) as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(PilotRecord)
                .where(PilotRecord.kind == "weather_hour")
            )
            == 72
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(PilotRecord)
                .where(PilotRecord.kind == "weather_raw")
            )
            == 1
        )
    exported = export_day(settings, clean_database, date(2026, 9, 12))
    assert exported["hours"] == 24
    csv = (tmp_path / "datasets/2026-09-12/weather-hourly.csv").read_text()
    assert len(csv.splitlines()) == 25 and "historical_forecast_model" in csv
    raw["hourly"]["temperature_2m"][0] = None
    assert sync(settings, clean_database, now, force=True)["status"] == "attention"

    def failed(*a, **kw):
        raise httpx.ConnectError("private error")

    monkeypatch.setattr("health_agent.research.weather.httpx.get", failed)
    result = sync(settings, clean_database, now, force=True)
    assert result["safe_error"] == "weather_sync_failed"
    assert "private error" not in json.dumps(result)
    assert export_day(settings, clean_database, date(2026, 9, 12))["hours"] == 24
