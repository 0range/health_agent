"""A narrowly-owned macOS LaunchAgent for one-shot reminder dispatch."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from health_agent.automation.storage import (
    atomic_private_write,
    private_directory,
    reject_symlink_components,
    require_private_file,
)

REMINDER_LABEL = "com.orange.health-agent.reminders"
REMINDER_START_INTERVAL_SECONDS = 60
REMINDER_LOG_ROTATE_BYTES = 5_242_880


class ReminderLaunchdError(RuntimeError):
    """Content-free operational failure safe for the CLI boundary."""


class Launchctl(Protocol):
    def run(self, arguments: tuple[str, ...]) -> int: ...


class SystemLaunchctl:
    def run(self, arguments: tuple[str, ...]) -> int:
        try:
            completed = subprocess.run(
                ("launchctl", *arguments),
                shell=False,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise ReminderLaunchdError("launchctl_unavailable") from error
        return completed.returncode


@dataclass(frozen=True, slots=True)
class ReminderLaunchdPaths:
    automation_root: Path
    executable: Path
    environment_file: Path
    working_directory: Path
    rendered_plist: Path
    installed_plist: Path
    stdout_log: Path
    stderr_log: Path
    lock_file: Path

    @classmethod
    def resolve(
        cls,
        *,
        automation_root: Path,
        executable: Path,
        environment_file: Path,
        working_directory: Path,
        home: Path | None = None,
    ) -> ReminderLaunchdPaths:
        expanded_root = automation_root.expanduser()
        reject_symlink_components(expanded_root)
        root = expanded_root.resolve()
        executable_path = executable.expanduser().resolve()
        environment = require_private_file(environment_file.expanduser()).resolve()
        working = working_directory.expanduser().resolve()
        if (
            not executable_path.is_absolute()
            or not executable_path.is_file()
            or not os.access(executable_path, os.X_OK)
        ):
            raise ValueError("executable_unavailable")
        if not working.is_absolute() or not working.is_dir():
            raise ValueError("working_directory_unavailable")
        owner_home = (home or Path.home()).expanduser().resolve()
        return cls(
            automation_root=root,
            executable=executable_path,
            environment_file=environment,
            working_directory=working,
            rendered_plist=root / "launchd" / f"{REMINDER_LABEL}.plist",
            installed_plist=(
                owner_home / "Library" / "LaunchAgents" / f"{REMINDER_LABEL}.plist"
            ),
            stdout_log=root / "logs" / "reminders-stdout.log",
            stderr_log=root / "logs" / "reminders-stderr.log",
            lock_file=root / "reminders.lock",
        )


class ReminderLaunchdManager:
    def __init__(
        self,
        paths: ReminderLaunchdPaths,
        *,
        launchctl: Launchctl | None = None,
        platform: str | None = None,
        uid: int | None = None,
    ) -> None:
        self.paths = paths
        self.launchctl = launchctl or SystemLaunchctl()
        self.platform = platform or sys.platform
        self.uid = os.getuid() if uid is None else uid

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def service(self) -> str:
        return f"{self.domain}/{REMINDER_LABEL}"

    def render(self) -> Path:
        private_directory(self.paths.automation_root)
        private_directory(self.paths.rendered_plist.parent)
        private_directory(self.paths.stdout_log.parent)
        payload = {
            "Label": REMINDER_LABEL,
            "ProgramArguments": [
                str(self.paths.executable),
                "reminder",
                "dispatch",
                "--env-file",
                str(self.paths.environment_file),
            ],
            "WorkingDirectory": str(self.paths.working_directory),
            "StartInterval": REMINDER_START_INTERVAL_SECONDS,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(self.paths.stdout_log),
            "StandardErrorPath": str(self.paths.stderr_log),
        }
        atomic_private_write(
            self.paths.rendered_plist,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True),
        )
        return self.paths.rendered_plist

    def is_loaded(self) -> bool:
        self._require_macos()
        return self.launchctl.run(("print", self.service)) == 0

    def install(self) -> str:
        self._require_macos()
        rendered = self.render()
        rotate_reminder_logs(self.paths)
        content = rendered.read_bytes()
        previous = self._installed_bytes()
        loaded = self.is_loaded()
        _write_installed(self.paths.installed_plist, content)
        if loaded and previous == content:
            return "installed"
        if loaded and self.launchctl.run(("bootout", self.service)) != 0:
            self._restore(previous)
            raise ReminderLaunchdError("launchctl_bootout_failed")
        if (
            self.launchctl.run(
                ("bootstrap", self.domain, str(self.paths.installed_plist))
            )
            != 0
        ):
            self._restore(previous)
            if loaded and previous is not None:
                self.launchctl.run(
                    ("bootstrap", self.domain, str(self.paths.installed_plist))
                )
            raise ReminderLaunchdError("launchctl_bootstrap_failed")
        return "installed"

    def status(self) -> str:
        return "loaded" if self.is_loaded() else "unloaded"

    def stop(self) -> str:
        self._require_macos()
        if not self.is_loaded():
            return "stopped"
        if self.launchctl.run(("bootout", self.service)) != 0:
            raise ReminderLaunchdError("launchctl_bootout_failed")
        return "stopped"

    def remove(self) -> str:
        self._require_macos()
        self.stop()
        for path in (self.paths.installed_plist, self.paths.rendered_plist):
            if path.is_symlink():
                raise ReminderLaunchdError("unsafe_plist_path")
            if path.exists():
                if not path.is_file():
                    raise ReminderLaunchdError("unsafe_plist_path")
                path.unlink()
        return "removed"

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise ReminderLaunchdError("launchd_requires_macos")

    def _installed_bytes(self) -> bytes | None:
        path = self.paths.installed_plist
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ReminderLaunchdError("unsafe_plist_path")
        return path.read_bytes() if path.exists() else None

    def _restore(self, content: bytes | None) -> None:
        path = self.paths.installed_plist
        if content is None:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
            return
        _write_installed(path, content)


def _write_installed(path: Path, content: bytes) -> None:
    reject_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise ReminderLaunchdError("unsafe_plist_path")
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def rotate_reminder_logs(paths: ReminderLaunchdPaths) -> None:
    private_directory(paths.stdout_log.parent)
    for path in (paths.stdout_log, paths.stderr_log):
        rotated = path.with_name(f"{path.name}.1")
        for candidate in (path, rotated):
            if candidate.is_symlink() or (
                candidate.exists() and not candidate.is_file()
            ):
                raise ReminderLaunchdError("unsafe_log_path")
        if path.exists() and path.stat().st_size > REMINDER_LOG_ROTATE_BYTES:
            if rotated.exists():
                rotated.unlink()
            os.replace(path, rotated)
            rotated.chmod(0o600)
        flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except OSError as error:
            raise ReminderLaunchdError("unsafe_log_path") from error
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
