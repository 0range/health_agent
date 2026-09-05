from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from health_agent.reminders.launchd import (
    REMINDER_LABEL,
    REMINDER_START_INTERVAL_SECONDS,
    ReminderLaunchdError,
    ReminderLaunchdManager,
    ReminderLaunchdPaths,
)


class FakeLaunchctl:
    def __init__(self) -> None:
        self.loaded = False
        self.calls: list[tuple[str, ...]] = []

    def run(self, arguments: tuple[str, ...]) -> int:
        self.calls.append(arguments)
        if arguments[0] == "print":
            return 0 if self.loaded else 113
        if arguments[0] == "bootstrap":
            self.loaded = True
            return 0
        if arguments[0] == "bootout":
            self.loaded = False
            return 0
        return 1


def _paths(tmp_path: Path) -> ReminderLaunchdPaths:
    executable = tmp_path / "bin" / "health-agent"
    executable.parent.mkdir(parents=True)
    executable.write_text("executable", encoding="utf-8")
    executable.chmod(0o700)
    env_file = tmp_path / "private.env"
    env_file.write_text("DATABASE_URL=postgresql://SECRET\n", encoding="utf-8")
    env_file.chmod(0o600)
    working = tmp_path / "repo"
    working.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return ReminderLaunchdPaths.resolve(
        automation_root=working / "data" / "automation",
        executable=executable,
        environment_file=env_file,
        working_directory=working,
        home=home,
    )


def test_render_is_exact_secret_free_and_every_minute(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    rendered = ReminderLaunchdManager(paths, platform="darwin", uid=501).render()
    payload = plistlib.loads(rendered.read_bytes())

    assert payload == {
        "Label": REMINDER_LABEL,
        "ProcessType": "Background",
        "ProgramArguments": [
            str(paths.executable),
            "reminder",
            "dispatch",
            "--env-file",
            str(paths.environment_file),
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(paths.stderr_log),
        "StandardOutPath": str(paths.stdout_log),
        "StartInterval": REMINDER_START_INTERVAL_SECONDS,
        "WorkingDirectory": str(paths.working_directory),
    }
    assert REMINDER_START_INTERVAL_SECONDS == 60
    assert b"SECRET" not in rendered.read_bytes()
    assert stat.S_IMODE(rendered.stat().st_mode) == 0o600


def test_install_stop_remove_are_idempotent_and_narrow(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    launchctl = FakeLaunchctl()
    manager = ReminderLaunchdManager(
        paths, launchctl=launchctl, platform="darwin", uid=501
    )
    neighbor = paths.installed_plist.parent / "neighbor.plist"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("keep", encoding="utf-8")

    assert manager.install() == "installed"
    assert manager.install() == "installed"
    assert [call[0] for call in launchctl.calls].count("bootstrap") == 1
    assert manager.status() == "loaded"
    assert manager.stop() == "stopped"
    assert manager.remove() == "removed"
    assert neighbor.read_text(encoding="utf-8") == "keep"
    assert not paths.installed_plist.exists()
    assert not paths.rendered_plist.exists()


def test_lifecycle_requires_macos_but_render_does_not(tmp_path: Path) -> None:
    manager = ReminderLaunchdManager(_paths(tmp_path), platform="linux")
    assert manager.render().exists()
    with pytest.raises(ReminderLaunchdError, match="launchd_requires_macos"):
        manager.install()


def test_rejects_non_private_environment_file(tmp_path: Path) -> None:
    executable = tmp_path / "health-agent"
    executable.write_text("x", encoding="utf-8")
    executable.chmod(0o700)
    env_file = tmp_path / "public.env"
    env_file.write_text("SECRET=yes", encoding="utf-8")
    env_file.chmod(0o644)
    working = tmp_path / "repo"
    working.mkdir()

    with pytest.raises(ValueError, match="env_file_must_be_private"):
        ReminderLaunchdPaths.resolve(
            automation_root=working / "automation",
            executable=executable,
            environment_file=env_file,
            working_directory=working,
        )
