"""Private checkpoint and locking primitives for background runs."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from health_agent.automation.models import AutomationJob

FULL_INTERVAL = timedelta(days=7)


def reject_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise RuntimeError("unsafe_path")


def private_directory(path: Path) -> Path:
    reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise RuntimeError("unsafe_path")
    path.chmod(0o700)
    return path


def require_private_file(path: Path) -> Path:
    if not path.is_absolute():
        raise ValueError("env_file_not_absolute")
    reject_symlink_components(path)
    try:
        info = path.lstat()
    except OSError as error:
        raise ValueError("env_file_unavailable") from error
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise ValueError("env_file_must_be_private")
    return path


def atomic_private_write(path: Path, content: bytes) -> None:
    private_directory(path.parent)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise RuntimeError("unsafe_path")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        directory_descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    finally:
        temporary.unlink(missing_ok=True)


class AutomationState:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> dict[str, Any]:
        if self.path.is_symlink():
            raise RuntimeError("unsafe_path")
        if not self.path.exists():
            return {"full_success": {}}
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("unsafe_path")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("full_success", {}), dict):
            raise TypeError("invalid_state")
        return payload

    @staticmethod
    def _key(job: AutomationJob) -> str:
        return json.dumps(job.key, separators=(",", ":"))

    def full_due(self, job: AutomationJob, now: datetime) -> bool:
        if not job.supports_full:
            return False
        value = self._read().get("full_success", {}).get(self._key(job))
        if value is None:
            return True
        timestamp = datetime.fromisoformat(str(value))
        if timestamp.tzinfo is None:
            raise ValueError("invalid_state")
        return now.astimezone(UTC) - timestamp.astimezone(UTC) >= FULL_INTERVAL

    def mark_full_success(self, job: AutomationJob, now: datetime) -> None:
        payload = self._read()
        checkpoints = payload.setdefault("full_success", {})
        checkpoints[self._key(job)] = now.astimezone(UTC).isoformat()
        atomic_private_write(
            self.path,
            (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(),
        )


class GlobalRunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._descriptor: int | None = None

    def acquire(self) -> bool:
        private_directory(self.path.parent)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except OSError as error:
            raise RuntimeError("lock_unavailable") from error
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return False
        except OSError:
            os.close(descriptor)
            raise RuntimeError("lock_unavailable") from None
        self._descriptor = descriptor
        return True

    def release(self) -> None:
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None

    def fileno(self) -> int:
        """Expose the held descriptor so a supervised child can retain the lock."""
        if self._descriptor is None:
            raise RuntimeError("lock_not_acquired")
        return self._descriptor
