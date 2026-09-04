from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from health_agent.automation.models import AutomationResult
from health_agent.cli import app


class FakeRunner:
    def __init__(self, results: tuple[AutomationResult, ...]) -> None:
        self.results = results
        self.force_full: bool | None = None

    def run(self, force_full: bool = False) -> tuple[AutomationResult, ...]:
        self.force_full = force_full
        return self.results


class FakeManager:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[str] = []

    def render(self) -> Path:
        self.calls.append("render")
        return self.root / "com.orange.health-agent.sync.plist"

    def install(self) -> str:
        self.calls.append("install")
        return "installed"

    def status(self) -> str:
        self.calls.append("status")
        return "loaded"

    def stop(self) -> str:
        self.calls.append("stop")
        return "stopped"

    def remove(self) -> str:
        self.calls.append("remove")
        return "removed"


class FakePaths:
    def __init__(self, root: Path) -> None:
        self.stdout_log = root / "stdout.log"
        self.stderr_log = root / "stderr.log"


def test_sync_prints_safe_results_continues_and_exits_nonzero(
    monkeypatch, tmp_path: Path
) -> None:
    runner = FakeRunner(
        (
            AutomationResult(
                "gmail", "profile-1", "main", "full", "failed", "connector_failed"
            ),
            AutomationResult(
                "drive", "profile-2", "main", "incremental", "succeeded"
            ),
        )
    )
    manager = FakeManager(tmp_path)
    monkeypatch.setattr(
        "health_agent.cli._automation_components",
        lambda path: (runner, manager, FakePaths(tmp_path)),
    )
    monkeypatch.setattr("health_agent.cli.rotate_safe_logs", lambda paths: None)
    result = CliRunner().invoke(
        app,
        ["automation", "sync", "--env-file", str(tmp_path / "env"), "--full"],
    )
    assert result.exit_code == 1
    assert runner.force_full is True
    assert "source=gmail profile=profile-1 account=main mode=full status=failed" in result.stdout
    assert "source=drive profile=profile-2 account=main mode=incremental status=succeeded" in result.stdout
    assert "summary jobs=2 succeeded=1 deferred=0 failed=1 timed_out=0 skipped=0" in result.stdout


def test_already_running_and_deferred_are_nonfatal(monkeypatch, tmp_path: Path) -> None:
    results = (
        AutomationResult("runner", "none", "none", "none", "skipped", "already_running"),
        AutomationResult(
            "whoop", "profile-1", "main", "full", "deferred", "connector_deferred"
        ),
    )
    monkeypatch.setattr(
        "health_agent.cli._automation_components",
        lambda path: (FakeRunner(results), FakeManager(tmp_path), FakePaths(tmp_path)),
    )
    monkeypatch.setattr("health_agent.cli.rotate_safe_logs", lambda paths: None)
    result = CliRunner().invoke(
        app, ["automation", "sync", "--env-file", str(tmp_path / "env")]
    )
    assert result.exit_code == 0
    assert "deferred=1" in result.stdout and "skipped=1" in result.stdout


def test_lifecycle_commands_route_through_manager(monkeypatch, tmp_path: Path) -> None:
    manager = FakeManager(tmp_path)
    monkeypatch.setattr(
        "health_agent.cli._automation_components",
        lambda path: (FakeRunner(()), manager, FakePaths(tmp_path)),
    )
    runner = CliRunner()
    for command, expected in (
        ("render", "status=rendered"),
        ("install", "status=installed"),
        ("status", "status=loaded"),
        ("stop", "status=stopped"),
        ("remove", "status=removed"),
    ):
        result = runner.invoke(
            app, ["automation", command, "--env-file", str(tmp_path / "env")]
        )
        assert result.exit_code == 0, result.output
        assert expected in result.stdout
    assert manager.calls == ["render", "install", "status", "stop", "remove"]


def test_real_cli_rejects_public_env_without_leaking_value(tmp_path: Path) -> None:
    env_file = tmp_path / "public.env"
    env_file.write_text("SECRET_VALUE=not-visible", encoding="utf-8")
    env_file.chmod(0o644)
    result = CliRunner().invoke(
        app, ["automation", "render", "--env-file", str(env_file)]
    )
    assert result.exit_code == 1
    assert "safe_error=automation_configuration_failed" in result.stderr
    assert "not-visible" not in result.stdout + result.stderr


def test_real_cli_rejects_relative_env_path_without_echoing_it() -> None:
    result = CliRunner().invoke(
        app, ["automation", "render", "--env-file", "private.env"]
    )
    assert result.exit_code == 1
    assert "safe_error=automation_configuration_failed" in result.stderr
    assert "private.env" not in result.stdout + result.stderr
