"""Symlink-safe, profile-scoped local connector storage."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from health_agent.google_drive.config import validate_profile_id
from health_agent.google_sheets.config import SheetsProfile
from health_agent.google_sheets.types import SheetsAccountIdentity


def _private_directory(path: Path) -> Path:
    path = Path(path)
    if path.exists():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise RuntimeError("unsafe Sheets connector directory")
    else:
        path.mkdir(parents=True, mode=0o700)
    path.chmod(0o700)
    return path


def _profile_directory(root: Path, profile_id: str) -> Path:
    root = _private_directory(root)
    return _private_directory(root / validate_profile_id(profile_id))


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError("refusing symlinked Sheets connector file")
    if not path.exists():
        if default is not None:
            return dict(default)
        raise FileNotFoundError(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
            raise RuntimeError("Sheets connector file must be a private regular file")
        value = json.load(stream)
    if not isinstance(value, dict):
        raise TypeError("Sheets connector file must contain a JSON object")
    return value


def _atomic_private_write(path: Path, value: str) -> None:
    _private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeError("refusing symlinked Sheets connector file")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    _private_directory(path.parent)
    descriptor = os.open(
        path,
        os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class LocalSheetsProfileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "profile.json"

    def save(self, profile: SheetsProfile) -> None:
        _atomic_private_write(
            self.path_for(profile.profile_id),
            json.dumps(profile.to_dict(), sort_keys=True, indent=2),
        )

    def load(self, profile_id: str) -> SheetsProfile:
        expected = validate_profile_id(profile_id)
        profile = SheetsProfile.from_dict(_read_json(self.path_for(expected)))
        if profile.profile_id != expected:
            raise ValueError("stored Sheets profile belongs to another profile")
        return profile

    def exists(self, profile_id: str) -> bool:
        return self.path_for(profile_id).is_file()


class LocalSheetsTokenStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "token.json"

    def publish_verified(
        self,
        profile_id: str,
        identity: SheetsAccountIdentity,
        token_json: str,
    ) -> Path:
        profile_id = validate_profile_id(profile_id)
        credentials = json.loads(token_json)
        if not isinstance(credentials, dict):
            raise TypeError("OAuth token must be a JSON object")
        permission_id = identity.permission_id.strip()
        email = identity.email.strip().casefold()
        if not permission_id or "@" not in email:
            raise ValueError("verified Sheets identity is invalid")
        with _exclusive_lock(_private_directory(self.root) / "authorizations.lock"):
            own_path = self.path_for(profile_id)
            if own_path.exists():
                own = _read_json(own_path)
                if own.get("account_permission_id") != permission_id:
                    raise ValueError("Sheets profile is already bound to another Google account")
            for candidate in self.root.glob("*/token.json"):
                stored = _read_json(candidate)
                if (
                    stored.get("account_permission_id") == permission_id
                    and stored.get("profile_id") != profile_id
                ):
                    raise ValueError("Google account is already bound to another health profile")
            payload = {
                "profile_id": profile_id,
                "account_permission_id": permission_id,
                "account_email": email,
                "credentials": credentials,
            }
            _atomic_private_write(own_path, json.dumps(payload, sort_keys=True))
            return own_path

    def load_verified(
        self, profile_id: str
    ) -> tuple[SheetsAccountIdentity, dict[str, Any]] | None:
        path = self.path_for(profile_id)
        if not path.exists():
            return None
        value = _read_json(path)
        expected = validate_profile_id(profile_id)
        if value.get("profile_id") != expected:
            raise ValueError("stored Sheets token belongs to another health profile")
        credentials = value.get("credentials")
        permission_id = value.get("account_permission_id")
        email = value.get("account_email")
        if not isinstance(credentials, dict) or not isinstance(permission_id, str) or not isinstance(email, str):
            raise TypeError("stored Sheets authorization is incomplete")
        return SheetsAccountIdentity(permission_id, email), credentials

    def exists(self, profile_id: str) -> bool:
        path = self.path_for(profile_id)
        return not path.is_symlink() and path.is_file()


class LocalSheetsStateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def sync_lock(self, profile_id: str):  # type: ignore[no-untyped-def]
        return _exclusive_lock(_profile_directory(self.root, profile_id) / "sync.lock")

    def read(self, profile_id: str) -> dict[str, Any]:
        return _read_json(
            _profile_directory(self.root, profile_id) / "sync-state.json", {}
        )

    def write(self, profile_id: str, value: dict[str, Any]) -> None:
        _atomic_private_write(
            _profile_directory(self.root, profile_id) / "sync-state.json",
            json.dumps(value, sort_keys=True, indent=2),
        )

