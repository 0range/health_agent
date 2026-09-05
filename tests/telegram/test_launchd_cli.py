from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from health_agent import cli
from health_agent.telegram.launchd import (
    TELEGRAM_LABEL,
    TelegramLaunchdError,
    TelegramServiceResult,
)


class Manager:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp_path = tmp_path
        self.calls: list[str] = []

    def render(self) -> Path:
        self.calls.append("render")
        return self.tmp_path / f"{TELEGRAM_LABEL}.plist"

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


def test_launchd_commands_route_to_exact_manager_without_live_launchctl(
    monkeypatch, tmp_path: Path
) -> None:
    manager = Manager(tmp_path)
    received: list[Path] = []

    def build(path: Path) -> Manager:
        received.append(path)
        return manager

    monkeypatch.setattr(cli, "_telegram_launchd_manager", build, raising=False)
    runner = CliRunner()
    for command, expected in (
        ("render", "rendered"),
        ("install", "installed"),
        ("automation-status", "loaded"),
        ("stop", "stopped"),
        ("remove", "removed"),
    ):
        result = runner.invoke(
            cli.app,
            ["telegram", command, "--env-file", str(tmp_path / "private.env")],
        )
        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == f"status={expected} label={TELEGRAM_LABEL}"
    assert manager.calls == ["render", "install", "status", "stop", "remove"]
    assert received == [tmp_path / "private.env"] * 5


def test_service_run_reports_singleton_and_safe_child_outcomes(
    monkeypatch, tmp_path: Path
) -> None:
    results = iter(
        (
            TelegramServiceResult("already_running", 0),
            TelegramServiceResult("stopped", 0),
            TelegramServiceResult("failed", 7),
        )
    )
    received: list[Path] = []

    class Service:
        def run(self) -> TelegramServiceResult:
            return next(results)

    def build(path: Path) -> Service:
        received.append(path)
        return Service()

    monkeypatch.setattr(cli, "_telegram_service_runner", build, raising=False)
    runner = CliRunner()
    command = [
        "telegram",
        "service-run",
        "--env-file",
        str(tmp_path / "private.env"),
    ]

    duplicate = runner.invoke(cli.app, command)
    stopped = runner.invoke(cli.app, command)
    failed = runner.invoke(cli.app, command)

    assert duplicate.exit_code == 0
    assert duplicate.stdout.strip() == "status=skipped safe_error=already_running"
    assert stopped.exit_code == 0 and stopped.stdout.strip() == "status=stopped"
    assert failed.exit_code == 7
    assert failed.stderr.strip() == "status=blocked error=telegram_service_failed"
    assert received == [tmp_path / "private.env"] * 3


def test_configuration_and_lifecycle_failures_are_content_free(
    monkeypatch, tmp_path: Path
) -> None:
    secret = "private-token-and-medical-text"

    def fail(*_args, **_kwargs):
        raise ValueError(secret)

    monkeypatch.setattr(cli, "_telegram_launchd_manager", fail, raising=False)
    monkeypatch.setattr(cli, "_telegram_service_runner", fail, raising=False)
    runner = CliRunner()
    env_file = tmp_path / "private.env"
    for command, expected in (
        ("render", "status=failed safe_error=telegram_launchd_failed"),
        ("service-run", "status=blocked error=telegram_service_configuration_failed"),
    ):
        result = runner.invoke(
            cli.app,
            ["telegram", command, "--env-file", str(env_file)],
        )
        assert result.exit_code == 1
        assert expected in result.stderr
        assert secret not in result.output


def test_failed_rollback_surfaces_only_distinct_safe_code(
    monkeypatch, tmp_path: Path
) -> None:
    secret = "private-token-and-medical-text"

    class FailingManager(Manager):
        def install(self) -> str:
            try:
                raise RuntimeError(secret)
            except RuntimeError as cause:
                raise TelegramLaunchdError(
                    "launchctl_rollback_bootstrap_failed",
                    previous_service_restored=False,
                ) from cause

    monkeypatch.setattr(
        cli,
        "_telegram_launchd_manager",
        lambda _path: FailingManager(tmp_path),
        raising=False,
    )

    result = CliRunner().invoke(
        cli.app,
        ["telegram", "install", "--env-file", str(tmp_path / "private.env")],
    )

    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "status=failed safe_error=launchctl_rollback_bootstrap_failed"
    )
    assert "previous_service_restored" not in result.output
    assert secret not in result.output


def test_unrecognized_launchd_error_code_is_not_echoed(
    monkeypatch, tmp_path: Path
) -> None:
    secret = "private-token-and-medical-text"

    class FailingManager(Manager):
        def install(self) -> str:
            raise TelegramLaunchdError(secret)

    monkeypatch.setattr(
        cli,
        "_telegram_launchd_manager",
        lambda _path: FailingManager(tmp_path),
        raising=False,
    )

    result = CliRunner().invoke(
        cli.app,
        ["telegram", "install", "--env-file", str(tmp_path / "private.env")],
    )

    assert result.exit_code == 1
    assert result.stderr.strip() == (
        "status=failed safe_error=telegram_launchd_failed"
    )
    assert secret not in result.output


def test_real_component_builder_rejects_relative_or_public_env_before_child(
    monkeypatch, tmp_path: Path
) -> None:
    executable = tmp_path / "health-agent"
    executable.write_text("x", encoding="utf-8")
    executable.chmod(0o700)
    monkeypatch.setattr(cli, "_current_console_script", lambda: executable)
    runner = CliRunner()

    relative = runner.invoke(
        cli.app, ["telegram", "service-run", "--env-file", "private.env"]
    )
    public = tmp_path / "public.env"
    public.write_text("OPENAI_API_KEY=SECRET\n", encoding="utf-8")
    public.chmod(0o644)
    non_private = runner.invoke(
        cli.app,
        ["telegram", "service-run", "--env-file", str(public)],
    )

    assert relative.exit_code == non_private.exit_code == 1
    assert "telegram_service_configuration_failed" in relative.stderr
    assert "telegram_service_configuration_failed" in non_private.stderr
    assert "SECRET" not in non_private.output
