from __future__ import annotations

import plistlib
import stat
from pathlib import Path

import pytest

from health_agent.automation.launchd import (
    LABEL,
    LOG_ROTATE_BYTES,
    LaunchdError,
    LaunchdManager,
    LaunchdPaths,
    rotate_safe_logs,
)


class FakeLaunchctl:
    def __init__(self, loaded: bool = False) -> None:
        self.loaded = loaded
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


def _paths(tmp_path: Path) -> LaunchdPaths:
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
    return LaunchdPaths.resolve(
        automation_root=working / "data" / "automation",
        executable=executable,
        environment_file=env_file,
        working_directory=working,
        home=home,
    )


def test_render_exact_secret_free_private_plist(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    plist_path = LaunchdManager(paths, platform="darwin", uid=501).render()
    payload = plistlib.loads(plist_path.read_bytes())
    assert payload == {
        "Label": LABEL,
        "ProcessType": "Background",
        "ProgramArguments": [
            str(paths.executable),
            "automation",
            "sync",
            "--env-file",
            str(paths.environment_file),
        ],
        "RunAtLoad": True,
        "StandardErrorPath": str(paths.stderr_log),
        "StandardOutPath": str(paths.stdout_log),
        "StartInterval": 14400,
        "WorkingDirectory": str(paths.working_directory),
    }
    assert b"SECRET" not in plist_path.read_bytes()
    assert stat.S_IMODE(plist_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(paths.automation_root.stat().st_mode) == 0o700


def test_install_is_idempotent_and_stop_retains_files(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    launchctl = FakeLaunchctl()
    manager = LaunchdManager(paths, launchctl=launchctl, platform="darwin", uid=501)
    assert manager.install() == "installed"
    assert launchctl.calls[-1] == ("bootstrap", "gui/501", str(paths.installed_plist))
    assert stat.S_IMODE(paths.installed_plist.stat().st_mode) == 0o600
    parent_mode = stat.S_IMODE(paths.installed_plist.parent.stat().st_mode)
    manager.install()
    assert [call[0] for call in launchctl.calls].count("bootstrap") == 1
    assert stat.S_IMODE(paths.installed_plist.parent.stat().st_mode) == parent_mode
    assert manager.status() == "loaded"
    assert manager.stop() == "stopped"
    assert paths.installed_plist.exists() and paths.rendered_plist.exists()
    assert manager.status() == "unloaded"


def test_remove_deletes_only_exact_managed_plists(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    launchctl = FakeLaunchctl()
    manager = LaunchdManager(paths, launchctl=launchctl, platform="darwin", uid=501)
    manager.install()
    neighbor = paths.installed_plist.parent / "other.plist"
    neighbor.write_text("keep", encoding="utf-8")
    assert manager.remove() == "removed"
    assert not paths.installed_plist.exists()
    assert not paths.rendered_plist.exists()
    assert neighbor.read_text(encoding="utf-8") == "keep"


def test_lifecycle_fails_clearly_off_macos_but_render_remains_available(tmp_path: Path) -> None:
    manager = LaunchdManager(_paths(tmp_path), launchctl=FakeLaunchctl(), platform="linux")
    assert manager.render().exists()
    with pytest.raises(LaunchdError, match="launchd_requires_macos"):
        manager.install()


def test_rotates_only_above_five_mib_and_retains_one_private_generation(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    paths.stdout_log.parent.mkdir(parents=True)
    paths.stdout_log.write_bytes(b"x" * (LOG_ROTATE_BYTES + 1))
    paths.stderr_log.write_bytes(b"small")
    paths.stdout_log.with_name("stdout.log.1").write_text("old", encoding="utf-8")
    rotate_safe_logs(paths)
    assert paths.stdout_log.stat().st_size == 0
    assert paths.stdout_log.with_name("stdout.log.1").stat().st_size == LOG_ROTATE_BYTES + 1
    assert paths.stderr_log.read_bytes() == b"small"
    for path in (paths.stdout_log, paths.stderr_log, paths.stdout_log.with_name("stdout.log.1")):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_rejects_non_private_env_and_symlinked_managed_targets(tmp_path: Path) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("SAFE=1", encoding="utf-8")
    env_file.chmod(0o644)
    executable = tmp_path / "tool"
    executable.write_text("tool", encoding="utf-8")
    working = tmp_path / "repo"
    working.mkdir()
    with pytest.raises(ValueError, match="env_file_must_be_private"):
        LaunchdPaths.resolve(
            automation_root=working / "data",
            executable=executable,
            environment_file=env_file,
            working_directory=working,
        )

    private_target = tmp_path / "private-target"
    private_target.write_text("SAFE=1", encoding="utf-8")
    private_target.chmod(0o600)
    symlink = tmp_path / "env-link"
    symlink.symlink_to(private_target)
    with pytest.raises((RuntimeError, ValueError), match="unsafe_path"):
        LaunchdPaths.resolve(
            automation_root=working / "data",
            executable=executable,
            environment_file=symlink,
            working_directory=working,
        )

    paths = _paths(tmp_path / "safe")
    paths.stdout_log.parent.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("private", encoding="utf-8")
    paths.stdout_log.symlink_to(outside)
    with pytest.raises(LaunchdError, match="unsafe_log_path"):
        rotate_safe_logs(paths)
