"""Secret-free rendering and lifecycle management for a user LaunchAgent."""

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

LABEL = "com.orange.health-agent.sync"
START_INTERVAL_SECONDS = 14400
LOG_ROTATE_BYTES = 5_242_880


class LaunchdError(RuntimeError):
    """Content-free operational failure safe for the CLI boundary."""


class Launchctl(Protocol):
    def run(self, arguments: tuple[str, ...]) -> int: ...


class SystemLaunchctl:
    def run(self, arguments: tuple[str, ...]) -> int:
        try:
            result = subprocess.run(
                ("launchctl", *arguments),
                shell=False,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as error:
            raise LaunchdError("launchctl_unavailable") from error
        return result.returncode


@dataclass(frozen=True, slots=True)
class LaunchdPaths:
    automation_root: Path
    executable: Path
    environment_file: Path
    working_directory: Path
    rendered_plist: Path
    installed_plist: Path
    stdout_log: Path
    stderr_log: Path
    state_file: Path
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
    ) -> LaunchdPaths:
        expanded_root = automation_root.expanduser()
        reject_symlink_components(expanded_root)
        resolved_root = expanded_root.resolve()
        resolved_executable = executable.expanduser().resolve()
        expanded_env = environment_file.expanduser()
        require_private_file(expanded_env)
        resolved_env = expanded_env.resolve()
        resolved_working = working_directory.expanduser().resolve()
        if (
            not resolved_executable.is_file()
            or not resolved_executable.is_absolute()
            or not os.access(resolved_executable, os.X_OK)
        ):
            raise ValueError("executable_unavailable")
        if not resolved_working.is_dir() or not resolved_working.is_absolute():
            raise ValueError("working_directory_unavailable")
        reject_symlink_components(resolved_root)
        launchd_root = resolved_root / "launchd"
        logs_root = resolved_root / "logs"
        owner_home = (home or Path.home()).expanduser().resolve()
        return cls(
            automation_root=resolved_root,
            executable=resolved_executable,
            environment_file=resolved_env,
            working_directory=resolved_working,
            rendered_plist=launchd_root / f"{LABEL}.plist",
            installed_plist=owner_home / "Library" / "LaunchAgents" / f"{LABEL}.plist",
            stdout_log=logs_root / "stdout.log",
            stderr_log=logs_root / "stderr.log",
            state_file=resolved_root / "state.json",
            lock_file=resolved_root / "sync.lock",
        )


def _atomic_installed_write(path: Path, content: bytes) -> None:
    reject_symlink_components(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LaunchdError("unsafe_plist_path")
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


class LaunchdManager:
    def __init__(
        self,
        paths: LaunchdPaths,
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
        return f"{self.domain}/{LABEL}"

    def _plist_bytes(self) -> bytes:
        payload = {
            "Label": LABEL,
            "ProgramArguments": [
                str(self.paths.executable),
                "automation",
                "sync",
                "--env-file",
                str(self.paths.environment_file),
            ],
            "WorkingDirectory": str(self.paths.working_directory),
            "StartInterval": START_INTERVAL_SECONDS,
            "RunAtLoad": True,
            "ProcessType": "Background",
            "StandardOutPath": str(self.paths.stdout_log),
            "StandardErrorPath": str(self.paths.stderr_log),
        }
        return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=True)

    def render(self) -> Path:
        private_directory(self.paths.automation_root)
        private_directory(self.paths.rendered_plist.parent)
        private_directory(self.paths.stdout_log.parent)
        atomic_private_write(self.paths.rendered_plist, self._plist_bytes())
        return self.paths.rendered_plist

    def _require_macos(self) -> None:
        if self.platform != "darwin":
            raise LaunchdError("launchd_requires_macos")

    def is_loaded(self) -> bool:
        self._require_macos()
        return self.launchctl.run(("print", self.service)) == 0

    def install(self) -> str:
        self._require_macos()
        rendered = self.render()
        rotate_safe_logs(self.paths)
        rendered_bytes = rendered.read_bytes()
        previous_bytes = self._installed_bytes()
        loaded = self.is_loaded()
        _atomic_installed_write(self.paths.installed_plist, rendered_bytes)
        if loaded and previous_bytes == rendered_bytes:
            return "installed"
        if loaded and self.launchctl.run(("bootout", self.service)) != 0:
            self._restore_installed(previous_bytes)
            raise LaunchdError("launchctl_bootout_failed")
        if self.launchctl.run(("bootstrap", self.domain, str(self.paths.installed_plist))) != 0:
            self._restore_installed(previous_bytes)
            if loaded and previous_bytes is not None:
                self.launchctl.run(
                    ("bootstrap", self.domain, str(self.paths.installed_plist))
                )
            raise LaunchdError("launchctl_bootstrap_failed")
        return "installed"

    def _installed_bytes(self) -> bytes | None:
        path = self.paths.installed_plist
        if path.is_symlink():
            raise LaunchdError("unsafe_plist_path")
        if not path.exists():
            return None
        if not path.is_file():
            raise LaunchdError("unsafe_plist_path")
        return path.read_bytes()

    def _restore_installed(self, content: bytes | None) -> None:
        path = self.paths.installed_plist
        if content is None:
            if path.exists() and path.is_file() and not path.is_symlink():
                path.unlink()
            return
        _atomic_installed_write(path, content)

    def status(self) -> str:
        return "loaded" if self.is_loaded() else "unloaded"

    def stop(self) -> str:
        self._require_macos()
        if not self.is_loaded():
            return "stopped"
        if self.launchctl.run(("bootout", self.service)) != 0:
            raise LaunchdError("launchctl_bootout_failed")
        return "stopped"

    def remove(self) -> str:
        self._require_macos()
        self.stop()
        for path in (self.paths.installed_plist, self.paths.rendered_plist):
            if path.is_symlink():
                raise LaunchdError("unsafe_plist_path")
            if path.exists():
                if not path.is_file():
                    raise LaunchdError("unsafe_plist_path")
                path.unlink()
        return "removed"


def _create_private_log(path: Path) -> None:
    flags = os.O_CREAT | os.O_APPEND | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise LaunchdError("unsafe_log_path") from error
    try:
        os.fchmod(descriptor, 0o600)
    finally:
        os.close(descriptor)


def rotate_safe_logs(paths: LaunchdPaths) -> None:
    private_directory(paths.stdout_log.parent)
    for path in (paths.stdout_log, paths.stderr_log):
        rotated = path.with_name(f"{path.name}.1")
        for candidate in (path, rotated):
            if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
                raise LaunchdError("unsafe_log_path")
        if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
            if rotated.exists():
                rotated.unlink()
            os.replace(path, rotated)
            rotated.chmod(0o600)
        _create_private_log(path)
