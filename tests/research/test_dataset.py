import csv
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from health_agent.automation.storage import atomic_private_write
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.pilot.storage import PilotStore
from health_agent.qingping.service import Connection, Placement
from health_agent.research.calendar import bounds
from health_agent.research.dataset import export_day, interval_summary, stress_rows
from health_agent.research.participants import check_participants
from health_agent.whoop.details import DetailService
from health_agent.whoop.models import WhoopConnection


def test_half_open_intervals_keep_counts_offsets_and_missing_bins():
    start, end = bounds(date(2026, 9, 10))
    points = [
        (start, 60),
        (start + timedelta(seconds=1), 80),
        (start + timedelta(seconds=6), 90),
        (end, 999),
        (start - timedelta(seconds=1), 999),
        (start, float("nan")),
    ]
    bins = interval_summary(points, start, end)
    assert bins[0]["count"] == 2
    assert bins[0]["mean"] == 70
    assert bins[0]["last_at_utc"] == (start + timedelta(seconds=1)).isoformat()
    assert bins[1]["mean"] == 90
    assert 2 not in bins
    assert len(bins) == 2


def test_stress_labels_never_become_fabricated_utc_and_all_graphs_survive():
    graph = {
        "graph": {
            "plots": [
                {
                    "type": "LINE_PLOT",
                    "plot": {
                        "segments": [
                            {
                                "points": [
                                    {
                                        "position_x": 0.2,
                                        "position_y": 0.5,
                                        "data_scrubber_details": {
                                            "primary_contextual_display": "11:59 PM",
                                            "value_display": "1.5",
                                        },
                                    }
                                ]
                            }
                        ]
                    },
                }
            ]
        }
    }
    payload = {
        "logical_id": "2026-09-10",
        "raw": {
            k: graph
            for k in ["stress_graph", "extended24_hour_graph", "previous_stress_graph"]
        },
    }
    rows = stress_rows(payload, "person_1")
    assert len(rows) == 3
    assert all(r["at_utc"] == "" and r["native_time_label"] == "11:59 PM" for r in rows)
    assert all(r["displayed_score"] == "1.5" and r["position_y"] == 0.5 for r in rows)


def test_daily_export_keeps_profiles_separate_and_shared_humidity(
    clean_database, tmp_path, monkeypatch
):
    day = date(2026, 9, 10)
    start, _ = bounds(day)
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    second, unrelated = uuid4(), uuid4()
    manifest = tmp_path / "participants.json"
    atomic_private_write(
        manifest,
        json.dumps(
            [
                {
                    "profile_id": str(second),
                    "session_file": str(tmp_path / "second.json"),
                    "root": str(tmp_path / "second"),
                }
            ]
        ).encode(),
    )
    settings = Settings(
        _env_file=None,
        whoop_detail_accounts_file=manifest,
        research_root=tmp_path / "research",
        whoop_detail_root=tmp_path / "first",
        whoop_detail_session_file=tmp_path / "first.json",
        qingping_root=tmp_path / "air",
    )
    with session_scope(clean_database) as session:
        session.add_all(
            [
                Profile(id=second, name="Synthetic second"),
                Profile(id=unrelated, name="Synthetic unrelated"),
            ]
        )
        session.flush()
        for profile, uid in [(DEFAULT_PROFILE_ID, 7), (second, 8), (unrelated, 9)]:
            session.add(
                WhoopConnection(
                    profile_id=profile, account_name="main", external_user_id=uid
                )
            )
    store = PilotStore(clean_database)
    store.put(DEFAULT_PROFILE_ID, "shared", "sleep_context", "away", {
        "night_start_date": (day - timedelta(days=1)).isoformat(),
        "wake_date": day.isoformat(), "location": "away", "evidence": "reported",
        "source": "user", "timezone": "Europe/Moscow",
    })
    collector = DetailService(store, tmp_path)
    for profile, uid, value in [
        (DEFAULT_PROFILE_ID, 7, 60),
        (second, 8, 90),
        (unrelated, 9, 199),
    ]:
        collector.archive(
            SimpleNamespace(profile_id=profile, user_id=uid),
            "heart_rate",
            day.isoformat(),
            {"values": [{"time": start.timestamp() * 1000, "data": value}]},
            start,
            now,
        )
    sensor = Connection(
        profile_id=DEFAULT_PROFILE_ID,
        mac="AABBCCDDEEFF",
        start_at=start,
        placements=[Placement(since=start, room="bedroom")],
    )
    store.put(
        DEFAULT_PROFILE_ID,
        "shared",
        "room_measurement",
        "synthetic-air",
        {
            "device_mac": sensor.mac,
            "room": "bedroom",
            "metrics": {"humidity": {"value": 48.5, "unit": "%"}},
        },
        at=start,
    )
    store.put(
        second,
        "shared",
        "room_measurement",
        "foreign-air",
        {"device_mac": sensor.mac, "metrics": {"humidity": {"value": 99, "unit": "%"}}},
        at=start,
    )
    result = export_day(settings, clean_database, sensor, day, now)
    assert result["partial_day"]
    assert len(result["participants"]) == 2
    assert result["air_samples"] == 1
    root = settings.research_root / "datasets" / day.isoformat()
    context = json.loads((root / "night-context.json").read_text())
    assert context["person_1"][0]["room_comparison"] == "exclude_away"
    assert context["person_2"] == []
    assert "night-context.json" in result["files"]
    with (root / "series-6s.csv").open() as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 14400
    assert float(rows[0]["person_1_heart_rate_mean"]) == 60
    assert float(rows[0]["person_2_heart_rate_mean"]) == 90
    assert float(rows[0]["air_humidity_mean"]) == 48.5
    assert rows[1]["person_1_heart_rate_mean"] == ""
    assert rows[1]["person_1_heart_rate_count"] == "0"
    assert "person_3_heart_rate_mean" not in rows[0]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in root.iterdir())
    for name, evidence in result["files"].items():
        assert (
            hashlib.sha256((root / name).read_bytes()).hexdigest() == evidence["sha256"]
        )
    monkeypatch.setattr(
        "health_agent.research.quality.capacity", lambda *a: {"status": "ok"}
    )
    before_review = datetime(2026, 9, 11, 6, tzinfo=UTC)
    assert (
        check_participants(settings, clean_database, sensor, before_review)["datasets"]
        == []
    )
    after_review = before_review + timedelta(hours=1)
    review = check_participants(settings, clean_database, sensor, after_review)
    assert review["datasets"] == [
        {"date": day.isoformat(), "status": "exported", "rows": 14400}
    ]
    assert not json.loads((root / "manifest.json").read_text())["partial_day"]
