from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.pilot.storage import PilotStore
from health_agent.qingping.service import Connection, Placement
from health_agent.research.calendar import bounds, recent_days
from health_agent.research.quality import QualityService, coverage
from health_agent.whoop.details import DetailService


def test_moscow_days_include_previous_even_before_utc_midnight():
    now = datetime(2026, 9, 10, 22, tzinfo=UTC)
    assert recent_days(now, include_today=True) == [
        date(2026, 9, 10),
        date(2026, 9, 11),
    ]
    assert recent_days(now) == [date(2026, 9, 10)]
    assert bounds(date(2026, 9, 10)) == (
        datetime(2026, 9, 9, 21, tzinfo=UTC),
        datetime(2026, 9, 10, 21, tzinfo=UTC),
    )


def test_coverage_duplicates_do_not_hide_missing_time_and_edges_count():
    start, end = bounds(date(2026, 9, 11))
    full = [start + timedelta(seconds=n) for n in range(0, 86400, 6)]
    assert coverage(full, start, end)["status"] == "ok"
    lost = [
        t
        for t in full
        if t < start + timedelta(hours=1) or t >= start + timedelta(hours=2)
    ]
    result = coverage(lost + lost, start, end)
    assert result["status"] == "gaps"
    assert 95 < result["coverage_percent"] < 96
    assert result["largest_gap_seconds"] == 3606
    empty = coverage([], start, end)
    assert empty["coverage_percent"] == 0
    assert empty["largest_gap_seconds"] == 86400
    # Samples outside the requested calendar day never improve its coverage.
    assert coverage([start - timedelta(seconds=6), end], start, end)["samples"] == 0


def test_daily_report_is_profile_scoped_uses_latest_revision_and_marks_setup(
    clean_database, tmp_path
):
    start, end = bounds(date(2026, 9, 11))
    sensor = Connection(
        profile_id=DEFAULT_PROFILE_ID,
        mac="AABBCCDDEEFF",
        start_at=start,
        placements=[Placement(since=start, room="office")],
    )
    service = QualityService(
        clean_database, sensor, tmp_path, tmp_path, tmp_path, "synthetic"
    )
    store = PilotStore(clean_database)
    detail = DetailService(store, tmp_path)
    owner = SimpleNamespace(profile_id=DEFAULT_PROFILE_ID, user_id=7)
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Synthetic"))
    raw_a = {"values": [{"data": 60, "time": start.timestamp() * 1000}]}
    raw_b = {
        "values": [
            {"data": 61, "time": (start + timedelta(seconds=6)).timestamp() * 1000}
        ]
    }
    detail.archive(owner, "heart_rate", "2026-09-11", raw_a, start, end)
    detail.archive(
        owner, "heart_rate", "2026-09-11", raw_b, start, end + timedelta(hours=1)
    )
    detail.archive(
        owner, "heart_rate", "2026-09-11", raw_a, start, end + timedelta(hours=2)
    )
    detail.archive(
        SimpleNamespace(profile_id=other, user_id=8),
        "heart_rate",
        "2026-09-11",
        {"values": []},
        start,
        end + timedelta(hours=3),
    )
    result = service.day(date(2026, 9, 11), DEFAULT_PROFILE_ID, 7)
    assert result["status"] == "attention"
    assert result["heart_rate"]["samples"] == 1
    assert result["heart_rate"]["first"] == start.isoformat()
    assert result["air"]["samples"] == 0
    assert result["nightly"]["status"] == "missing"
    assert result["stress"]["status"] == "missing"
    assert (
        service.day(date(2026, 9, 10), DEFAULT_PROFILE_ID, 7)["status"] == "setup_day"
    )
    assert len(store.list(DEFAULT_PROFILE_ID, "shared", "whoop_detail")) == 2


def test_current_check_flags_empty_successful_ingestion(
    clean_database, tmp_path, monkeypatch
):
    import json

    from health_agent.automation.storage import atomic_private_write
    from health_agent.whoop.models import WhoopConnection

    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    start, _ = bounds(now.date())
    with session_scope(clean_database) as session:
        session.add(
            WhoopConnection(
                profile_id=DEFAULT_PROFILE_ID,
                account_name="synthetic",
                external_user_id=7,
                last_success_at=now,
            )
        )
    sensor = Connection(
        profile_id=DEFAULT_PROFILE_ID,
        mac="AABBCCDDEEFF",
        start_at=start,
        placements=[Placement(since=start, room="office")],
    )
    air_root, detail_root = tmp_path / "air", tmp_path / "detail"
    atomic_private_write(
        air_root / "state.json", json.dumps({"last_success": now.isoformat()}).encode()
    )
    atomic_private_write(
        detail_root / "state.json",
        json.dumps({"last_attempt": now.isoformat(), "errors": []}).encode(),
    )
    monkeypatch.setattr(
        "health_agent.research.quality.capacity", lambda *a: {"status": "ok"}
    )
    result = QualityService(
        clean_database,
        sensor,
        tmp_path / "research",
        detail_root,
        air_root,
        "synthetic",
    ).run(now)
    assert result["status"] == "attention"
    assert result["sources"]["air"]["status"] == "attention"
    assert result["sources"]["whoop_detail"]["status"] == "attention"
    assert result["sources"]["whoop_public"]["status"] == "ok"
    assert result["daily_reports"] == []


def test_storage_checks_container_limit_separately_from_host(
    clean_database, tmp_path, monkeypatch
):
    from health_agent.research.quality import GIB, capacity

    monkeypatch.setattr(
        "health_agent.research.quality.shutil.disk_usage",
        lambda _: SimpleNamespace(free=200 * GIB),
    )
    monkeypatch.setattr(
        "health_agent.research.quality.subprocess.run",
        lambda *a, **kw: SimpleNamespace(
            stdout="Filesystem 1024-blocks Used Available Capacity Mounted\n/dev/synthetic 60000000 59000000 1000000 99% /\n"
        ),
    )
    result = capacity(clean_database, tmp_path, "synthetic")
    assert result["status"] == "attention"
    assert result["forecast_31_days_two_whoops_bytes"] >= 10 * GIB
    assert result["host_free_bytes"] > result["database_filesystem_free_bytes"]
