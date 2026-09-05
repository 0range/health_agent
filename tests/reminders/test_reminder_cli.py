from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import Engine
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.reminders.dispatcher import DispatchReport
from health_agent.reminders.repository import ReminderRepository

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 5, 7, 0, tzinfo=UTC)


def _settings(engine: Engine) -> Settings:
    return Settings(database_url=engine.url.render_as_string(hide_password=False))


def test_propose_status_and_lifecycle_cli_are_profile_scoped(
    clean_database: Engine, monkeypatch
) -> None:
    monkeypatch.setattr(
        "health_agent.cli.Settings", lambda *a, **k: _settings(clean_database)
    )
    runner = CliRunner()
    proposed = runner.invoke(
        app,
        [
            "reminder",
            "propose",
            str(PROFILE_ID),
            "--title",
            "Repeat ferritin",
            "--reason",
            "Doctor requested a repeat",
            "--when",
            "2026-09-06T10:00",
            "--source-type",
            "doctor_note",
            "--source-reference",
            "document:abc",
        ],
    )
    assert proposed.exit_code == 0, proposed.output
    assert "status=pending_confirmation" in proposed.stdout
    code = proposed.stdout.split("code=", 1)[1].split()[0]

    status = runner.invoke(app, ["reminder", "status", str(PROFILE_ID)])
    confirmed = runner.invoke(app, ["reminder", "confirm", str(PROFILE_ID), code])
    done = runner.invoke(app, ["reminder", "complete", str(PROFILE_ID), code])

    assert "pending_confirmation=1" in status.stdout
    assert confirmed.exit_code == 0 and "status=scheduled" in confirmed.stdout
    assert done.exit_code == 0 and "status=completed" in done.stdout


def test_dispatch_cli_uses_global_lock_and_safe_counts(
    monkeypatch, tmp_path: Path
) -> None:
    class Dispatcher:
        def run(self):
            return DispatchReport(proposals_sent=1, due_acknowledged=1)

    class Lock:
        acquired = True

        def acquire(self):
            return self.acquired

        def release(self):
            self.acquired = False

    env_file = tmp_path / "private.env"
    env_file.write_text("SAFE=1", encoding="utf-8")
    env_file.chmod(0o600)
    lock = Lock()
    monkeypatch.setattr(
        "health_agent.cli._reminder_dispatch_components",
        lambda path: (
            Dispatcher(),
            lock,
            SimpleNamespace(
                stdout_log=tmp_path / "stdout.log",
                stderr_log=tmp_path / "stderr.log",
            ),
        ),
    )
    monkeypatch.setattr("health_agent.cli.rotate_reminder_logs", lambda paths: None)

    result = CliRunner().invoke(
        app, ["reminder", "dispatch", "--env-file", str(env_file)]
    )

    assert result.exit_code == 0, result.output
    assert "status=succeeded proposals_sent=1" in result.stdout
    assert "due_acknowledged=1" in result.stdout
    assert not lock.acquired


def test_launchd_cli_routes_without_installing(monkeypatch, tmp_path: Path) -> None:
    class Manager:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def render(self):
            self.calls.append("render")
            return tmp_path / "com.orange.health-agent.reminders.plist"

        def install(self):
            self.calls.append("install")
            return "installed"

        def status(self):
            self.calls.append("status")
            return "loaded"

        def stop(self):
            self.calls.append("stop")
            return "stopped"

        def remove(self):
            self.calls.append("remove")
            return "removed"

    manager = Manager()
    monkeypatch.setattr("health_agent.cli._reminder_launchd_manager", lambda _: manager)
    runner = CliRunner()
    for command, expected in (
        ("render", "rendered"),
        ("install", "installed"),
        ("automation-status", "loaded"),
        ("stop", "stopped"),
        ("remove", "removed"),
    ):
        result = runner.invoke(
            app, ["reminder", command, "--env-file", str(tmp_path / "env")]
        )
        assert result.exit_code == 0, result.output
        assert f"status={expected}" in result.stdout


def test_list_never_reads_another_profile(clean_database: Engine, monkeypatch) -> None:
    monkeypatch.setattr(
        "health_agent.cli.Settings", lambda *a, **k: _settings(clean_database)
    )
    with session_scope(clean_database) as session:
        ReminderRepository(session).propose(
            profile_id=PROFILE_ID,
            title="Private title",
            reason="Private reason",
            source_type="user",
            source_reference="telegram",
            due_at=NOW,
            timezone_name="UTC",
            now=NOW,
            public_code="private-code",
        )
    other = UUID("00000000-0000-0000-0000-000000000002")
    result = CliRunner().invoke(app, ["reminder", "list", str(other)])
    assert result.exit_code == 0
    assert "Private title" not in result.stdout
