"""Always-on, narrowly-owned macOS LaunchAgent for the Telegram poller."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

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
_SAFE_ERROR_CODES = frozenset(
    {
        "launchctl_unavailable",
        "telegram_child_unavailable",
        "launchctl_status_failed",
        "launchctl_bootout_failed",
        "launchctl_bootstrap_failed",
        "launchctl_rollback_bootstrap_failed",
        "launchd_requires_macos",
        "unsafe_plist_path",
        "telegram_lifecycle_busy",
        "telegram_service_already_running",
        "unsafe_log_path",
        "telegram_launchd_failed",
    }
)


class TelegramLaunchdError(RuntimeError):
    """Content-free launchd/service failure safe for the CLI boundary."""

    def __init__(
        self,
        safe_code: str,
        *,
        previous_service_restored: bool = False,
    ) -> None:
        bounded_code = (
            safe_code if safe_code in _SAFE_ERROR_CODES else "telegram_launchd_failed"
        )
        super().__init__(bounded_code)
        self.safe_code = bounded_code
        self.previous_service_restored = previous_service_restored


class Launchctl(Protocol):
    def run(self, arguments: tuple[str, ...]) -> int: ...


class ChildProcess(Protocol):
    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        lock_descriptor: int,
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
    """Run the existing safe CLI against the post-rotation active log files."""

    def __init__(self, paths: TelegramLaunchdPaths) -> None:
        self.paths = paths

    def run(
        self,
        arguments: tuple[str, ...],
        *,
        cwd: Path,
        environment: dict[str, str],
        lock_descriptor: int,
    ) -> int:
        try:
            with (
                _open_private_log(self.paths.stdout_log) as stdout,
                _open_private_log(self.paths.stderr_log) as stderr,
            ):
                completed = subprocess.run(
                    arguments,
                    cwd=cwd,
                    env=environment,
                    stdout=stdout,
                    stderr=stderr,
                    pass_fds=(lock_descriptor,),
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
    lifecycle_lock_file: Path

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
            lifecycle_lock_file=root / "telegram-lifecycle.lock",
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
        self.child = child or SystemChildProcess(paths)
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
                lock_descriptor=self.lock.fileno(),
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
        lifecycle_lock: GlobalRunLock | None = None,
    ) -> None:
        self.paths = paths
        self.launchctl = launchctl or SystemLaunchctl()
        self.platform = platform or sys.platform
        self.uid = os.getuid() if uid is None else uid
        self.lifecycle_lock = lifecycle_lock or GlobalRunLock(
            paths.lifecycle_lock_file
        )

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}"

    @property
    def service(self) -> str:
        return f"{self.domain}/{TELEGRAM_LABEL}"

    def render(self) -> Path:
        with self._lifecycle_operation():
            return self._render_locked()

    def _render_locked(self) -> Path:
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
        with self._lifecycle_operation():
            return self._is_loaded_locked()

    def _is_loaded_locked(self) -> bool:
        result = self.launchctl.run(("print", self.service))
        if result == 0:
            return True
        if result == 113:
            return False
        raise TelegramLaunchdError("launchctl_status_failed")

    def install(self) -> str:
        self._require_macos()
        with self._lifecycle_operation():
            rendered = self._render_locked()
            content = rendered.read_bytes()
            previous = self._installed_bytes()
            loaded = self._is_loaded_locked()
            if loaded and previous == content:
                return "installed"
            _write_installed(self.paths.installed_plist, content)
            if loaded and self.launchctl.run(("bootout", self.service)) != 0:
                self._restore(previous)
                raise TelegramLaunchdError(
                    "launchctl_bootout_failed",
                    previous_service_restored=True,
                )
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
                restored = self._rollback(previous, loaded)
                raise TelegramLaunchdError(
                    "launchctl_bootstrap_failed",
                    previous_service_restored=restored,
                )
            return "installed"

    def status(self) -> str:
        self._require_macos()
        with self._lifecycle_operation():
            return "loaded" if self._is_loaded_locked() else "unloaded"

    def stop(self) -> str:
        self._require_macos()
        with self._lifecycle_operation():
            return self._stop_locked()

    def _stop_locked(self) -> str:
        if not self._is_loaded_locked():
            return "stopped"
        if self.launchctl.run(("bootout", self.service)) != 0:
            raise TelegramLaunchdError("launchctl_bootout_failed")
        return "stopped"

    def remove(self) -> str:
        self._require_macos()
        with self._lifecycle_operation():
            for path in (self.paths.installed_plist, self.paths.rendered_plist):
                reject_symlink_components(path)
                if path.is_symlink() or (path.exists() and not path.is_file()):
                    raise TelegramLaunchdError("unsafe_plist_path")
            self._stop_locked()
            for path in (self.paths.installed_plist, self.paths.rendered_plist):
                path.unlink(missing_ok=True)
            return "removed"

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise TelegramLaunchdError("launchd_requires_macos")

    def _installed_bytes(self) -> bytes | None:
        path = self.paths.installed_plist
        reject_symlink_components(path)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise TelegramLaunchdError("unsafe_plist_path")
        if not path.exists():
            return None
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise TelegramLaunchdError("unsafe_plist_path")
        return path.read_bytes()

    def _restore(self, content: bytes | None) -> None:
        path = self.paths.installed_plist
        if content is None:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
            return
        _write_installed(path, content)

    def _rollback(self, previous: bytes | None, was_loaded: bool) -> bool:
        self._restore(previous)
        if was_loaded and previous is not None:
            if (
                self.launchctl.run(
                    ("bootstrap", self.domain, str(self.paths.installed_plist))
                )
                != 0
            ):
                raise TelegramLaunchdError(
                    "launchctl_rollback_bootstrap_failed",
                    previous_service_restored=False,
                )
            return True
        return False

    @contextmanager
    def _lifecycle_operation(self) -> Iterator[None]:
        if not self.lifecycle_lock.acquire():
            raise TelegramLaunchdError("telegram_lifecycle_busy")
        try:
            yield
        finally:
            self.lifecycle_lock.release()


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
        if rotated.exists():
            rotated.chmod(0o600)
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


def _open_private_log(path: Path) -> BinaryIO:
    flags = os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise TelegramLaunchdError("unsafe_log_path")
        return os.fdopen(descriptor, "ab")
    except BaseException:
        if "descriptor" in locals():
            os.close(descriptor)
        raise


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
