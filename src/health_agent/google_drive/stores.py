"""Small local stores for profile settings, OAuth tokens, and sync state."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from health_agent.google_drive.config import DriveProfile, validate_profile_id
from health_agent.google_drive.types import DriveAccountIdentity, SeenItem


def _reject_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise RuntimeError(f"Refusing symlinked connector directory: {component}")


def _private_directory(path: Path) -> Path:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"Refusing non-directory connector path: {path}")
    path.chmod(0o700)
    return path


def _profile_directory(root: Path, profile_id: str) -> Path:
    return _private_directory(Path(root) / validate_profile_id(profile_id))


def _atomic_private_write(path: Path, content: str) -> None:
    _private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked connector state file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"Refusing non-regular connector state file: {path}")
    if not path.exists():
        return default
    if not path.is_file():
        raise RuntimeError(f"Refusing non-regular connector state file: {path}")
    path.chmod(0o600)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"Connector state must be a JSON object: {path}")
    return loaded


@contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    _private_directory(path.parent)
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


class LocalProfileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "profile.json"

    def save(self, profile: DriveProfile) -> None:
        path = self._path(profile.profile_id)
        _atomic_private_write(path, json.dumps(profile.to_dict(), sort_keys=True, indent=2))

    def load(self, profile_id: str) -> DriveProfile:
        path = self._path(profile_id)
        if not path.exists():
            raise FileNotFoundError(f"Google Drive profile {profile_id!r} is not configured")
        profile = DriveProfile.from_dict(_read_json(path, {}))
        if profile.profile_id != validate_profile_id(profile_id):
            raise ValueError("stored profile ID does not match its directory")
        return profile

    def exists(self, profile_id: str) -> bool:
        return self._path(profile_id).exists()


class LocalTokenStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "token.json"

    def publish_verified(
        self,
        profile_id: str,
        identity: DriveAccountIdentity,
        token_json: str,
    ) -> Path:
        profile_id = validate_profile_id(profile_id)
        credentials = json.loads(token_json)
        if not isinstance(credentials, dict):
            raise TypeError("OAuth token must be a JSON object")
        permission_id = identity.permission_id.strip()
        email = identity.email.strip().casefold()
        if not permission_id or not email or "@" not in email:
            raise ValueError("verified Google Drive identity is invalid")
        with _exclusive_file_lock(_private_directory(self.root) / "authorizations.lock"):
            own_path = self.path_for(profile_id)
            if own_path.exists():
                existing_own = _read_json(own_path, {})
                if existing_own.get("account_permission_id") != permission_id:
                    raise ValueError(
                        "Drive profile is already bound to another Google account"
                    )
            for existing_path in self.root.glob("*/token.json"):
                if existing_path.is_symlink():
                    raise RuntimeError("Refusing symlinked Drive OAuth token")
                existing = _read_json(existing_path, {})
                if (
                    existing.get("account_permission_id") == permission_id
                    and existing.get("profile_id") != profile_id
                ):
                    raise ValueError(
                        "Google Drive account is already bound to another health profile"
                    )
            payload = {
                "profile_id": profile_id,
                "account_permission_id": permission_id,
                "account_email": email,
                "credentials": credentials,
            }
            path = own_path
            _atomic_private_write(path, json.dumps(payload, sort_keys=True))
            return path

    def load_verified(
        self, profile_id: str
    ) -> tuple[DriveAccountIdentity, dict[str, Any]] | None:
        path = self.path_for(profile_id)
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked Drive OAuth token")
        if not path.exists():
            return None
        payload = _read_json(path, {})
        expected = validate_profile_id(profile_id)
        if payload.get("profile_id") != expected:
            raise ValueError("stored Drive token belongs to another health profile")
        permission_id = payload.get("account_permission_id")
        email = payload.get("account_email")
        credentials = payload.get("credentials")
        if not all(isinstance(value, str) for value in (permission_id, email)):
            raise TypeError("stored Drive authorization binding is incomplete")
        if not isinstance(credentials, dict):
            raise TypeError("stored Drive OAuth credentials are incomplete")
        return DriveAccountIdentity(str(permission_id), str(email)), credentials

    def exists(self, profile_id: str) -> bool:
        path = self.path_for(profile_id)
        if path.is_symlink():
            raise RuntimeError("Refusing symlinked Drive OAuth token")
        return path.is_file()


class LocalSyncStateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "sync-state.json"

    def _read(self, profile_id: str) -> dict[str, Any]:
        return _read_json(self._path(profile_id), {"cursor": None, "items": {}})

    def _write(self, profile_id: str, state: dict[str, Any]) -> None:
        _atomic_private_write(
            self._path(profile_id), json.dumps(state, sort_keys=True, indent=2)
        )

    def sync_lock(self, profile_id: str) -> AbstractContextManager[None]:
        return _exclusive_file_lock(_profile_directory(self.root, profile_id) / "sync.lock")

    def get_cursor(self, profile_id: str) -> str | None:
        cursor = self._read(profile_id).get("cursor")
        return None if cursor is None else str(cursor)

    def set_cursor(self, profile_id: str, cursor: str) -> None:
        state = self._read(profile_id)
        state["cursor"] = cursor
        self._write(profile_id, state)

    def clear_cursor(self, profile_id: str) -> None:
        state = self._read(profile_id)
        state["cursor"] = None
        self._write(profile_id, state)

    def get_seen(self, profile_id: str, file_id: str) -> SeenItem | None:
        value = self._read(profile_id).get("items", {}).get(file_id)
        if value is None:
            return None
        if value.get("profile_id") != validate_profile_id(profile_id):
            raise ValueError("stored sync item belongs to a different profile")
        value["ancestor_folder_ids"] = tuple(value.get("ancestor_folder_ids", ()))
        value["folder_path"] = tuple(value.get("folder_path", ()))
        return SeenItem(**value)

    def retryable_items(self, profile_id: str) -> tuple[SeenItem, ...]:
        items = self._read(profile_id).get("items", {})
        if not isinstance(items, dict):
            raise TypeError("stored Drive items must be an object")
        retryable: list[SeenItem] = []
        for file_id in sorted(items):
            item = self.get_seen(profile_id, file_id)
            if item is not None and item.status in {
                "transient_download_failed",
                "processing_failed",
            }:
                retryable.append(item)
        return tuple(retryable)

    def record_seen(self, item: SeenItem) -> None:
        profile_id = validate_profile_id(item.profile_id)
        state = self._read(profile_id)
        values = asdict(item)
        values["ancestor_folder_ids"] = list(item.ancestor_folder_ids)
        values["folder_path"] = list(item.folder_path)
        state.setdefault("items", {})[item.file_id] = values
        self._write(profile_id, state)

    def mark_removed(self, profile_id: str, file_id: str) -> bool:
        state = self._read(profile_id)
        item = state.get("items", {}).get(file_id)
        if item is None or item.get("status") == "removed":
            return False
        item["status"] = "removed"
        self._write(profile_id, state)
        return True

    def mark_tree_removed(self, profile_id: str, folder_id: str) -> int:
        state = self._read(profile_id)
        changed = 0
        for item in state.get("items", {}).values():
            ancestors = item.get("ancestor_folder_ids", [])
            if folder_id in ancestors and item.get("status") != "removed":
                item["status"] = "removed"
                changed += 1
        if changed:
            self._write(profile_id, state)
        return changed

    def mark_missing_removed(
        self, profile_id: str, root_folder_ids: tuple[str, ...], seen_file_ids: set[str]
    ) -> int:
        state = self._read(profile_id)
        del root_folder_ids
        changed = 0
        for file_id, item in state.get("items", {}).items():
            if (
                file_id not in seen_file_ids
                and item.get("status") != "removed"
            ):
                item["status"] = "removed"
                changed += 1
        if changed:
            self._write(profile_id, state)
        return changed

    def count_seen(self, profile_id: str) -> int:
        items = self._read(profile_id).get("items", {}).values()
        return sum(
            item.get("status") in {"medically_imported", "duplicate", "ocr_required"}
            for item in items
        )

    def counts(self, profile_id: str) -> dict[str, int]:
        result: dict[str, int] = {}
        for item in self._read(profile_id).get("items", {}).values():
            status = str(item.get("status", "unknown"))
            result[status] = result.get(status, 0) + 1
        return result

    def begin_sync(self, profile_id: str, mode: str) -> None:
        state = self._read(profile_id)
        run = state.setdefault("run", {})
        run["last_attempt_at"] = datetime.now(UTC).isoformat()
        run["last_mode"] = mode
        run["last_error_code"] = None
        run["in_progress"] = "yes"
        self._write(profile_id, state)

    def finish_sync(self, profile_id: str) -> None:
        state = self._read(profile_id)
        run = state.setdefault("run", {})
        run["last_success_at"] = datetime.now(UTC).isoformat()
        run["last_error_code"] = None
        run["root_accessible"] = "yes"
        run["in_progress"] = "no"
        self._write(profile_id, state)

    def fail_sync(self, profile_id: str, safe_error_code: str) -> None:
        state = self._read(profile_id)
        run = state.setdefault("run", {})
        run["last_error_code"] = safe_error_code
        run["root_accessible"] = "unknown"
        run["in_progress"] = "no"
        self._write(profile_id, state)

    def run_state(self, profile_id: str) -> dict[str, str | None]:
        run = self._read(profile_id).get("run", {})
        if not isinstance(run, dict):
            raise TypeError("stored Drive run state must be an object")
        return {
            "last_attempt_at": _optional_string(run.get("last_attempt_at")),
            "last_success_at": _optional_string(run.get("last_success_at")),
            "last_mode": _optional_string(run.get("last_mode")),
            "last_error_code": _optional_string(run.get("last_error_code")),
            "root_accessible": _optional_string(run.get("root_accessible")),
            "in_progress": _optional_string(run.get("in_progress")),
        }


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
