from __future__ import annotations

import os
import plistlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from health_agent.automation.launchd import LABEL as CONNECTOR_LABEL
from health_agent.automation.storage import GlobalRunLock
from health_agent.reminders.launchd import REMINDER_LABEL
from health_agent.telegram.launchd import (
    TELEGRAM_LABEL,
    TELEGRAM_LOG_ROTATE_BYTES,
    TelegramLaunchdError,
    TelegramLaunchdManager,
    TelegramLaunchdPaths,
    TelegramServiceRunner,
    rotate_telegram_logs,
)


class FakeLaunchctl:
    def __init__(self, *, loaded: bool = False) -> None:
        self.loaded = loaded
        self.calls: list[tuple[str, ...]] = []
        self.fail_bootstrap = False
        self.fail_bootout = False
        self.status_code: int | None = None

    def run(self, arguments: tuple[str, ...]) -> int:
        self.calls.append(arguments)
        if arguments[0] == "print":
            return self.status_code if self.status_code is not None else (0 if self.loaded else 113)
        if arguments[0] == "bootstrap":
            if self.fail_bootstrap:
                return 5
            self.loaded = True
            return 0
        if arguments[0] == "bootout":
            if self.fail_bootout:
                return 5
            self.loaded = False
            return 0
        return 1


def _paths(tmp_path: Path, *, env_name: str = "private.env") -> TelegramLaunchdPaths:
    executable = tmp_path / "bin" / "health-agent"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text("executable", encoding="utf-8")
    executable.chmod(0o700)
    environment_file = tmp_path / env_name
    environment_file.write_text("OPENAI_API_KEY=SECRET\n", encoding="utf-8")
    environment_file.chmod(0o600)
    working_directory = tmp_path / "repo"
    working_directory.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return TelegramLaunchdPaths.resolve(
        automation_root=working_directory / "data" / "automation",
        executable=executable,
        environment_file=environment_file,
        working_directory=working_directory,
        home=home,
    )


def test_render_is_exact_private_secret_free_and_isolated(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    rendered = TelegramLaunchdManager(paths, platform="darwin", uid=501).render()
    payload = plistlib.loads(rendered.read_bytes())

    assert payload == {
        "KeepAlive": True,
        "Label": TELEGRAM_LABEL,
        "ProcessType": "Background",
        "ProgramArguments": [
            str(paths.executable),
            "telegram",
            "service-run",
            "--env-file",
            str(paths.environment_file),
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(paths.stderr_log),
        "StandardOutPath": str(paths.stdout_log),
        "ThrottleInterval": 30,
        "Umask": 0o077,
        "WorkingDirectory": str(paths.working_directory),
    }
    assert TELEGRAM_LABEL not in {CONNECTOR_LABEL, REMINDER_LABEL}
    assert b"SECRET" not in rendered.read_bytes()
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.automation_root.stat().st_mode) == 0o700
    assert paths.lock_file.name == "telegram-service.lock"
    assert paths.stdout_log.name == "telegram-stdout.log"


def test_install_status_stop_remove_are_idempotent_and_narrow(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    launchctl = FakeLaunchctl()
    manager = TelegramLaunchdManager(
        paths, launchctl=launchctl, platform="darwin", uid=501
    )
    neighbor = paths.installed_plist.parent / f"{REMINDER_LABEL}.plist"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("keep", encoding="utf-8")

    assert manager.install() == "installed"
    assert manager.install() == "installed"
    assert [call[0] for call in launchctl.calls].count("bootstrap") == 1
    assert manager.status() == "loaded"
    assert stat.S_IMODE(paths.installed_plist.stat().st_mode) == 0o600
    assert manager.stop() == "stopped"
    assert manager.status() == "unloaded"
    assert manager.remove() == "removed"
    assert not paths.rendered_plist.exists()
    assert not paths.installed_plist.exists()
    assert neighbor.read_text(encoding="utf-8") == "keep"


def test_changed_loaded_configuration_reloads(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    first = _paths(tmp_path, env_name="first.env")
    TelegramLaunchdManager(first, launchctl=launchctl, platform="darwin").install()
    second_env = tmp_path / "second.env"
    second_env.write_text("SAFE=1\n", encoding="utf-8")
    second_env.chmod(0o600)
    second = TelegramLaunchdPaths.resolve(
        automation_root=first.automation_root,
        executable=first.executable,
        environment_file=second_env,
        working_directory=first.working_directory,
        home=tmp_path / "home",
    )

    TelegramLaunchdManager(second, launchctl=launchctl, platform="darwin").install()

    assert [call[0] for call in launchctl.calls].count("bootout") == 1
    assert [call[0] for call in launchctl.calls].count("bootstrap") == 2
    assert plistlib.loads(second.installed_plist.read_bytes())["ProgramArguments"][-1] == str(second_env)


def test_failed_changed_reload_restores_previous_loaded_service(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    first = _paths(tmp_path, env_name="first.env")
    manager = TelegramLaunchdManager(first, launchctl=launchctl, platform="darwin")
    manager.install()
    previous = first.installed_plist.read_bytes()
    second_env = tmp_path / "second.env"
    second_env.write_text("SAFE=1\n", encoding="utf-8")
    second_env.chmod(0o600)
    second = TelegramLaunchdPaths.resolve(
        automation_root=first.automation_root,
        executable=first.executable,
        environment_file=second_env,
        working_directory=first.working_directory,
        home=tmp_path / "home",
    )
    failing = TelegramLaunchdManager(second, launchctl=launchctl, platform="darwin")
    original_run = launchctl.run
    bootstrap_attempts = 0

    def fail_new_bootstrap(arguments: tuple[str, ...]) -> int:
        nonlocal bootstrap_attempts
        if arguments[0] == "bootstrap":
            bootstrap_attempts += 1
            if bootstrap_attempts == 1:
                return 5
        return original_run(arguments)

    launchctl.run = fail_new_bootstrap  # type: ignore[method-assign]
    with pytest.raises(TelegramLaunchdError, match="launchctl_bootstrap_failed"):
        failing.install()

    assert first.installed_plist.read_bytes() == previous
    assert launchctl.loaded


def test_status_unknown_failure_and_platform_errors_are_not_masked(tmp_path: Path) -> None:
    launchctl = FakeLaunchctl()
    launchctl.status_code = 5
    manager = TelegramLaunchdManager(
        _paths(tmp_path), launchctl=launchctl, platform="darwin"
    )
    with pytest.raises(TelegramLaunchdError, match="launchctl_status_failed"):
        manager.status()
    linux = TelegramLaunchdManager(_paths(tmp_path / "linux"), platform="linux")
    assert linux.render().exists()
    with pytest.raises(TelegramLaunchdError, match="launchd_requires_macos"):
        linux.install()


def test_paths_reject_relative_public_and_symlinked_environment(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    public = tmp_path / "public.env"
    public.write_text("SECRET=1", encoding="utf-8")
    public.chmod(0o644)
    linked = tmp_path / "linked.env"
    linked.symlink_to(paths.environment_file)
    common = {
        "automation_root": paths.automation_root,
        "executable": paths.executable,
        "working_directory": paths.working_directory,
    }
    with pytest.raises(ValueError, match="env_file_not_absolute"):
        TelegramLaunchdPaths.resolve(environment_file=Path("relative.env"), **common)
    with pytest.raises(ValueError, match="env_file_must_be_private"):
        TelegramLaunchdPaths.resolve(environment_file=public, **common)
    with pytest.raises((RuntimeError, ValueError), match="unsafe_path"):
        TelegramLaunchdPaths.resolve(environment_file=linked, **common)


def test_managed_plists_and_logs_fail_closed_on_hostile_targets(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    outside = tmp_path / "outside"
    outside.write_text("keep", encoding="utf-8")
    paths.rendered_plist.parent.mkdir(parents=True)
    paths.rendered_plist.symlink_to(outside)
    with pytest.raises(RuntimeError, match="unsafe_path"):
        TelegramLaunchdManager(paths, platform="darwin").render()
    paths.rendered_plist.unlink()
    paths.stdout_log.parent.mkdir(parents=True, exist_ok=True)
    paths.stdout_log.symlink_to(outside)
    with pytest.raises(TelegramLaunchdError, match="unsafe_log_path"):
        rotate_telegram_logs(paths)
    assert outside.read_text(encoding="utf-8") == "keep"


def test_loaded_install_rejects_non_private_managed_plist(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    launchctl = FakeLaunchctl()
    manager = TelegramLaunchdManager(
        paths, launchctl=launchctl, platform="darwin"
    )
    manager.install()
    paths.installed_plist.chmod(0o644)

    with pytest.raises(TelegramLaunchdError, match="unsafe_plist_path"):
        manager.install()


def test_log_rotation_is_private_bounded_and_refuses_running_service(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.stdout_log.parent.mkdir(parents=True)
    paths.stdout_log.write_bytes(b"x" * (TELEGRAM_LOG_ROTATE_BYTES + 1))
    paths.stderr_log.write_bytes(b"small")
    old = paths.stdout_log.with_name("telegram-stdout.log.1")
    old.write_text("old", encoding="utf-8")
    paths.stderr_log.with_name("telegram-stderr.log.1").write_text(
        "old", encoding="utf-8"
    )
    paths.stderr_log.with_name("telegram-stderr.log.1").chmod(0o644)

    rotate_telegram_logs(paths)

    assert paths.stdout_log.stat().st_size == 0
    assert old.stat().st_size == TELEGRAM_LOG_ROTATE_BYTES + 1
    assert paths.stderr_log.read_bytes() == b"small"
    for path in (
        paths.stdout_log,
        paths.stderr_log,
        old,
        paths.stderr_log.with_name("telegram-stderr.log.1"),
        paths.lock_file,
    ):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    held = GlobalRunLock(paths.lock_file)
    assert held.acquire()
    try:
        with pytest.raises(TelegramLaunchdError, match="telegram_service_already_running"):
            rotate_telegram_logs(paths)
    finally:
        held.release()


def test_runner_holds_singleton_uses_minimal_env_and_propagates_exit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    class Child:
        def run(
            self,
            arguments: tuple[str, ...],
            *,
            cwd: Path,
            environment: dict[str, str],
            lock_descriptor: int,
        ) -> int:
            calls.append((arguments, cwd, environment))
            assert lock_descriptor >= 0
            competing = TelegramServiceRunner(paths, child=self)
            assert competing.run().status == "already_running"
            return 7

    result = TelegramServiceRunner(paths, child=Child()).run()

    assert result.status == "failed" and result.returncode == 7
    arguments, cwd, environment = calls[0]
    assert arguments == (str(paths.executable), "telegram", "run")
    assert cwd == paths.working_directory
    assert environment == {
        "HEALTH_AGENT_ENV_FILE": str(paths.environment_file),
        "HOME": str(Path.home()),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    }
    retry = TelegramServiceRunner(paths, child=Child()).run()
    assert retry.status == "failed"


def test_system_child_reopens_active_logs_after_rotation(
    tmp_path: Path, monkeypatch
) -> None:
    paths = _paths(tmp_path)
    paths.stdout_log.parent.mkdir(parents=True)
    paths.stdout_log.write_bytes(b"x" * (TELEGRAM_LOG_ROTATE_BYTES + 1))
    captured: dict[str, object] = {}

    def run(*arguments, **kwargs):
        captured["arguments"] = arguments
        captured.update(kwargs)
        stdout_info = os.fstat(kwargs["stdout"].fileno())
        stderr_info = os.fstat(kwargs["stderr"].fileno())
        captured["stdout_identity"] = (stdout_info.st_dev, stdout_info.st_ino)
        captured["stderr_identity"] = (stderr_info.st_dev, stderr_info.st_ino)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("health_agent.telegram.launchd.subprocess.run", run)
    result = TelegramServiceRunner(paths).run()

    assert result.status == "stopped"
    stdout_path = paths.stdout_log.stat()
    stderr_path = paths.stderr_log.stat()
    assert captured["stdout_identity"] == (stdout_path.st_dev, stdout_path.st_ino)
    assert captured["stderr_identity"] == (stderr_path.st_dev, stderr_path.st_ino)
    assert captured["shell"] is False
    assert captured["pass_fds"]
    assert paths.stdout_log.with_name("telegram-stdout.log.1").stat().st_size == (
        TELEGRAM_LOG_ROTATE_BYTES + 1
    )


def test_runner_releases_lock_when_child_raises(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    class FailingChild:
        def run(self, *_args, **_kwargs) -> int:
            raise OSError("private child details")

    class SuccessfulChild:
        def run(self, *_args, **_kwargs) -> int:
            return 0

    with pytest.raises(OSError, match="private child details"):
        TelegramServiceRunner(paths, child=FailingChild()).run()
    assert TelegramServiceRunner(paths, child=SuccessfulChild()).run().status == "stopped"
