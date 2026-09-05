"""Always-on, narrowly-owned macOS LaunchAgent for the Telegram poller."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from health_agent.automation.storage import (
    GlobalRunLock,
    atomic_private_write,
    private_directory,
    reject_symlink_components,
    require_private_file,
)

TELEGRAM_LABEL = "com.orange.health-agent.telegram"
TELEGRAM_THROTTLE_SECONDS = 30
TELEGRAM_LOG_ROTATE_BYTES = 5_242_880
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


class TelegramLaunchdError(RuntimeError):
    """Content-free launchd/service failure safe for the CLI boundary."""


class Launchctl(Protocol):
    def run(self, arguments: tuple[str, ...]) -> int: ...


class ChildProcess(Protocol):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> int: ...


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
            raise TelegramLaunchdError("launchctl_unavailable") from error
        return completed.returncode


class SystemChildProcess:
    """Run the existing safe CLI while inheriting the LaunchAgent log streams."""

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
    ) -> int:
        try:
            completed = subprocess.run(
                arguments,
                cwd=cwd,
                env=environment,
                shell=False,
                check=False,
            )
        except OSError as error:
            raise TelegramLaunchdError("telegram_child_unavailable") from error
        return completed.returncode


@dataclass(frozen=True, slots=True)
class TelegramLaunchdPaths:
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
    ) -> TelegramLaunchdPaths:
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
            rendered_plist=root / "launchd" / f"{TELEGRAM_LABEL}.plist",
            installed_plist=(
                owner_home / "Library" / "LaunchAgents" / f"{TELEGRAM_LABEL}.plist"
            ),
            stdout_log=root / "logs" / "telegram-stdout.log",
            stderr_log=root / "logs" / "telegram-stderr.log",
            lock_file=root / "telegram-service.lock",
        )


@dataclass(frozen=True, slots=True)
class TelegramServiceResult:
    status: Literal["stopped", "failed", "already_running"]
    returncode: int


class TelegramServiceRunner:
    """Hold the singleton lock while the existing `telegram run` child lives."""

    def __init__(
        self,
        paths: TelegramLaunchdPaths,
        *,
        child: ChildProcess | None = None,
        lock: GlobalRunLock | None = None,
    ) -> None:
        self.paths = paths
        self.child = child or SystemChildProcess()
        self.lock = lock or GlobalRunLock(paths.lock_file)

    def run(self) -> TelegramServiceResult:
        if not self.lock.acquire():
            return TelegramServiceResult("already_running", 0)
        try:
            _rotate_telegram_logs_locked(self.paths)
            returncode = self.child.run(
                (str(self.paths.executable), "telegram", "run"),
                cwd=self.paths.working_directory,
                environment={
                    "HEALTH_AGENT_ENV_FILE": str(self.paths.environment_file),
                    "HOME": str(Path.home()),
                    "PATH": _MINIMAL_PATH,
                },
            )
        finally:
            self.lock.release()
        status: Literal["stopped", "failed"] = (
            "stopped" if returncode == 0 else "failed"
        )
        return TelegramServiceResult(status, returncode)


class TelegramLaunchdManager:
    def __init__(
        self,
        paths: TelegramLaunchdPaths,
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
        return f"{self.domain}/{TELEGRAM_LABEL}"

    def render(self) -> Path:
        private_directory(self.paths.automation_root)
        private_directory(self.paths.rendered_plist.parent)
        private_directory(self.paths.stdout_log.parent)
        payload = {
            "KeepAlive": True,
            "Label": TELEGRAM_LABEL,
            "ProcessType": "Background",
            "ProgramArguments": [
                str(self.paths.executable),
                "telegram",
                "service-run",
                "--env-file",
                str(self.paths.environment_file),
            ],
            "RunAtLoad": True,
            "StandardErrorPath": str(self.paths.stderr_log),
            "StandardOutPath": str(self.paths.stdout_log),
            "ThrottleInterval": TELEGRAM_THROTTLE_SECONDS,
            "Umask": 0o077,
            "WorkingDirectory": str(self.paths.working_directory),
        }
        atomic_private_write(
            self.paths.rendered_plist,
            plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True),
        )
        return self.paths.rendered_plist

    def is_loaded(self) -> bool:
        self._require_macos()
        result = self.launchctl.run(("print", self.service))
        if result == 0:
            return True
        if result == 113:
            return False
        raise TelegramLaunchdError("launchctl_status_failed")

    def install(self) -> str:
        self._require_macos()
        rendered = self.render()
        content = rendered.read_bytes()
        previous = self._installed_bytes()
        loaded = self.is_loaded()
        if loaded and previous == content:
            return "installed"
        _write_installed(self.paths.installed_plist, content)
        if loaded and self.launchctl.run(("bootout", self.service)) != 0:
            self._restore(previous)
            raise TelegramLaunchdError("launchctl_bootout_failed")
        try:
            rotate_telegram_logs(self.paths)
        except Exception:
            self._rollback(previous, loaded)
            raise
        if (
            self.launchctl.run(
                ("bootstrap", self.domain, str(self.paths.installed_plist))
            )
            != 0
        ):
            self._rollback(previous, loaded)
            raise TelegramLaunchdError("launchctl_bootstrap_failed")
        return "installed"

    def status(self) -> str:
        return "loaded" if self.is_loaded() else "unloaded"

    def stop(self) -> str:
        self._require_macos()
        if not self.is_loaded():
            return "stopped"
        if self.launchctl.run(("bootout", self.service)) != 0:
            raise TelegramLaunchdError("launchctl_bootout_failed")
        return "stopped"

    def remove(self) -> str:
        self._require_macos()
        for path in (self.paths.installed_plist, self.paths.rendered_plist):
            if path.is_symlink() or (path.exists() and not path.is_file()):
                raise TelegramLaunchdError("unsafe_plist_path")
        self.stop()
        for path in (self.paths.installed_plist, self.paths.rendered_plist):
            path.unlink(missing_ok=True)
        return "removed"

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise TelegramLaunchdError("launchd_requires_macos")

    def _installed_bytes(self) -> bytes | None:
        path = self.paths.installed_plist
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise TelegramLaunchdError("unsafe_plist_path")
        return path.read_bytes() if path.exists() else None

    def _restore(self, content: bytes | None) -> None:
        path = self.paths.installed_plist
        if content is None:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
            return
        _write_installed(path, content)

    def _rollback(self, previous: bytes | None, was_loaded: bool) -> None:
        self._restore(previous)
        if was_loaded and previous is not None:
            self.launchctl.run(
                ("bootstrap", self.domain, str(self.paths.installed_plist))
            )


def rotate_telegram_logs(paths: TelegramLaunchdPaths) -> None:
    """Rotate only after acquiring the same lock held for the poller's lifetime."""

    lock = GlobalRunLock(paths.lock_file)
    if not lock.acquire():
        raise TelegramLaunchdError("telegram_service_already_running")
    try:
        _rotate_telegram_logs_locked(paths)
    finally:
        lock.release()


def _rotate_telegram_logs_locked(paths: TelegramLaunchdPaths) -> None:
    private_directory(paths.stdout_log.parent)
    for path in (paths.stdout_log, paths.stderr_log):
        rotated = path.with_name(f"{path.name}.1")
        for candidate in (path, rotated):
            if candidate.is_symlink() or (
                candidate.exists() and not candidate.is_file()
            ):
                raise TelegramLaunchdError("unsafe_log_path")
        if path.exists() and path.stat().st_size > TELEGRAM_LOG_ROTATE_BYTES:
            if rotated.exists():
                rotated.unlink()
            os.replace(path, rotated)
            rotated.chmod(0o600)
        _create_private_log(path)


def _create_private_log(path: Path) -> None:
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise TelegramLaunchdError("unsafe_log_path") from error
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def _write_installed(path: Path, content: bytes) -> None:
    reject_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise TelegramLaunchdError("unsafe_plist_path")
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

