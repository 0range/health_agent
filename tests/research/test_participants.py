import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
import typer
from typer.testing import CliRunner

from health_agent.automation.storage import atomic_private_write
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.pilot.storage import PilotStore
from health_agent.qingping.service import Connection, Placement
from health_agent.research.participants import check_participants
from health_agent.whoop import details_cli
from health_agent.whoop.details import DetailService
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.participants import DetailTarget, targets


def configured(tmp_path, second):
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
    return Settings(
        _env_file=None,
        whoop_detail_accounts_file=manifest,
        whoop_detail_root=tmp_path / "first",
        whoop_detail_session_file=tmp_path / "first.json",
        research_root=tmp_path / "research",
        qingping_root=tmp_path / "air",
    )


def test_duplicate_session_path_rejected(tmp_path):
    settings = configured(tmp_path, uuid4())
    assert len(targets(settings)) == 2
    rows = json.loads(settings.whoop_detail_accounts_file.read_text())
    rows[0]["session_file"] = str(settings.whoop_detail_session_file)
    atomic_private_write(settings.whoop_detail_accounts_file, json.dumps(rows).encode())
    with pytest.raises(ValueError, match="duplicate_whoop_participant_path"):
        targets(settings)


def test_failed_first_sync_does_not_block_second(monkeypatch, tmp_path):
    enrolled = targets(configured(tmp_path, uuid4()))
    seen = []
    monkeypatch.setattr(details_cli, "configured_targets", lambda: enrolled)

    def run(target):
        seen.append(target)
        if target == enrolled[0]:
            raise typer.Exit(1)

    monkeypatch.setattr(details_cli, "sync_target", run)
    result = CliRunner().invoke(details_cli.app, ["sync"])
    assert result.exit_code == 1
    assert seen == enrolled


def test_wrong_profile_session_is_never_yielded(monkeypatch, tmp_path):
    target = DetailTarget(uuid4(), tmp_path / "session.json", tmp_path / "detail")
    monkeypatch.setattr(
        details_cli,
        "DetailClient",
        lambda *a: SimpleNamespace(profile_id=DEFAULT_PROFILE_ID),
    )
    with pytest.raises(typer.Exit), details_cli.operation(target):
        pytest.fail("mismatched session reached collector")
    assert (
        json.loads((target.root / "last_error.json").read_text())["error"]
        == "whoop_detail_profile_mismatch"
    )


def test_second_person_missing_hr_is_not_hidden_by_first_person(
    clean_database, tmp_path, monkeypatch
):
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    second = uuid4()
    settings = configured(tmp_path, second)
    with session_scope(clean_database) as session:
        session.add(Profile(id=second, name="Synthetic second"))
        session.flush()
        for pid, uid in ((DEFAULT_PROFILE_ID, 7), (second, 8)):
            session.add(
                WhoopConnection(
                    profile_id=pid,
                    account_name="main",
                    external_user_id=uid,
                    last_success_at=now,
                )
            )
    sensor = Connection(
        profile_id=DEFAULT_PROFILE_ID,
        mac="AABBCCDDEEFF",
        start_at=now,
        placements=[Placement(since=now, room="bedroom")],
    )
    for target in targets(settings):
        atomic_private_write(
            target.root / "state.json",
            json.dumps({"last_attempt": now.isoformat(), "errors": []}).encode(),
        )
    atomic_private_write(
        settings.qingping_root / "state.json",
        json.dumps({"last_success": now.isoformat()}).encode(),
    )
    store = PilotStore(clean_database)
    store.put(
        DEFAULT_PROFILE_ID,
        "shared",
        "room_measurement",
        "synthetic-air",
        {"device_mac": sensor.mac},
        at=now,
    )
    detail = DetailService(store, settings.whoop_detail_root)
    detail.archive(
        SimpleNamespace(profile_id=DEFAULT_PROFILE_ID, user_id=7),
        "heart_rate",
        "2026-09-10",
        {"values": [{"time": now.timestamp() * 1000, "data": 60}]},
        now,
        now,
    )
    monkeypatch.setattr(
        "health_agent.research.quality.capacity", lambda *a: {"status": "ok"}
    )
    result = check_participants(settings, clean_database, sensor, now)
    assert result["configured_whoops"] == 2
    assert result["enrolled_participants"] == 2
    assert result["participants"][0]["status"] == "ok"
    assert result["participants"][1]["sources"]["air"]["status"] == "ok"
    assert result["participants"][1]["sources"]["whoop_detail"]["status"] == "attention"
    assert result["status"] == "attention"
    detail.archive(
        SimpleNamespace(profile_id=second, user_id=8),
        "heart_rate",
        "2026-09-10",
        {"values": [{"time": now.timestamp() * 1000, "data": 70}]},
        now,
        now,
    )
    assert check_participants(settings, clean_database, sensor, now)["status"] == "ok"
