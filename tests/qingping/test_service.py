import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.storage import PilotRecord, PilotStore
from health_agent.qingping.client import QingpingError
from health_agent.qingping.service import (
    Connection,
    Placement,
    QingpingService,
    metrics,
)

START = datetime(2026, 9, 10, 0, tzinfo=UTC)
MAC = "AABBCCDDEEFF"


def measurement(at):
    return {
        "timestamp": {"value": at.timestamp()},
        "temperature": {"value": 21},
        "co2": {"value": 850},
        "tvoc_index": {"value": 53},
        "noise": {"value": 40},
    }


class Client:
    def __init__(self, rows):
        self.rows, self.calls = rows, []
        self.fail = False

    def devices(self):
        return [
            {
                "info": {
                    "mac": MAC,
                    "setting": {"report_interval": 60},
                    "status": {"offline": False},
                }
            }
        ]

    def history(self, mac, start, end):
        self.calls.append((start, end))
        if self.fail:
            raise QingpingError("qingping_api_unavailable")
        return [row for row in self.rows if start <= row["timestamp"]["value"] <= end]


def svc(engine, tmp_path):
    return QingpingService(
        PilotStore(engine),
        Connection(
            profile_id=DEFAULT_PROFILE_ID,
            mac=MAC,
            start_at=START,
            placements=[Placement(since=START, room="office")],
        ),
        tmp_path / "state.json",
    )


def test_replay_stores_raw_minutes_without_duplicates_and_reports_stale(
    clean_database, tmp_path
):
    service = svc(clean_database, tmp_path)
    client = Client([measurement(START + timedelta(minutes=n)) for n in (1, 2, 3)])
    for _ in range(2):
        result = service.sync(client, START + timedelta(minutes=4))
        assert result["samples"] == 3
        assert result["stale"] is False
    assert service.status(START + timedelta(minutes=9))["stale"] is True
    with session_scope(clean_database) as session:
        rows = session.scalars(
            select(PilotRecord).where(PilotRecord.kind == "room_measurement")
        ).all()
        assert len(rows) == 3
        assert rows[0].payload["raw"]["co2"]["value"] == 850
        assert rows[0].payload["metrics"]["tvoc_index"]["unit"] == "index"
    assert service.store.list(uuid4(), "shared", "room_measurement") == []


def test_move_relabels_existing_and_delayed_measurements_by_observed_time(
    clean_database, tmp_path
):
    service = svc(clean_database, tmp_path)
    client = Client([measurement(START + timedelta(minutes=n)) for n in (1, 3)])
    service.sync(client, START + timedelta(minutes=4))
    service.connection = Connection.model_validate(
        {
            **service.connection.model_dump(),
            "placements": [
                *service.connection.placements,
                Placement(since=START + timedelta(minutes=2), room="bedroom"),
            ],
        }
    )
    client.rows.append(measurement(START + timedelta(minutes=2)))
    service.sync(client, START + timedelta(minutes=5))
    rows = service.store.list(DEFAULT_PROFILE_ID, "shared", "room_measurement")
    assert {r.at.minute: r.payload["room"] for r in rows} == {
        1: "office",
        2: "bedroom",
        3: "bedroom",
    }
    assert service.status(START + timedelta(minutes=5))["room"] == "bedroom"


def test_failure_keeps_cursor_then_recovers_and_clears_error(clean_database, tmp_path):
    service = svc(clean_database, tmp_path)
    client = Client([measurement(START + timedelta(minutes=1))])
    service.sync(client, START + timedelta(minutes=2))
    previous = json.loads(service.state_path.read_text())["cursor"]
    client.fail = True
    with pytest.raises(QingpingError):
        service.sync(client, START + timedelta(minutes=3))
    state = json.loads(service.state_path.read_text())
    assert state["cursor"] == previous
    assert state["last_error"] == "qingping_api_unavailable"
    client.fail = False
    result = service.sync(client, START + timedelta(minutes=4))
    assert result["last_error"] is None
    assert result["samples"] == 1


def test_hourly_overlap_recovers_measurements_delayed_more_than_ten_minutes(
    clean_database, tmp_path
):
    service = svc(clean_database, tmp_path)
    client = Client([])
    service.sync(client, START + timedelta(hours=1))
    client.rows = [measurement(START + timedelta(minutes=1))]
    assert service.sync(client, START + timedelta(hours=1, minutes=1))["samples"] == 0
    assert service.sync(client, START + timedelta(hours=2))["samples"] == 1


def test_wrong_device_and_out_of_window_records_do_not_advance_cursor(
    clean_database, tmp_path
):
    service = svc(clean_database, tmp_path)
    client = Client([])
    client.devices = list
    with pytest.raises(QingpingError, match="qingping_device_not_found"):
        service.sync(client, START + timedelta(minutes=2))
    assert "cursor" not in json.loads(service.state_path.read_text())
    client = Client([])
    client.history = lambda *args: [measurement(START - timedelta(days=1))]
    with pytest.raises(QingpingError, match="qingping_history_outside_window"):
        service.sync(client, START + timedelta(minutes=2))
    assert service.status()["samples"] == 0


def test_missing_or_invalid_values_are_not_zero_and_raw_index_is_not_concentration():
    result = metrics(
        {
            "co2": {"value": None},
            "noise": {"value": float("nan")},
            "pm25": {"value": False},
            "tvoc_index": {"value": 10},
            "temperature": {"value": 0},
        }
    )
    assert result == {
        "tvoc_index": {"value": 10, "unit": "index"},
        "temperature": {"value": 0, "unit": "°C"},
    }


def test_sensor_faults_are_not_measurements_but_battery_charging_is_valid():
    assert metrics(
        {
            "co2": {"status": 2, "value": 65535},
            "noise": {"status": 1, "value": 99},
            "battery": {"status": 1, "value": 95},
        }
    ) == {"battery": {"value": 95, "unit": "%"}}
