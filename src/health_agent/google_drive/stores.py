"""Small local stores for profile settings, OAuth tokens, and sync state."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from health_agent.google_drive.config import DriveProfile, validate_profile_id
from health_agent.google_drive.types import SeenItem


def _profile_directory(root: Path, profile_id: str) -> Path:
    directory = Path(root) / validate_profile_id(profile_id)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _atomic_private_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
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
    if not path.exists():
        return default
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"Refusing non-regular connector state file: {path}")
    path.chmod(0o600)
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise TypeError(f"Connector state must be a JSON object: {path}")
    return loaded


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

    def save(self, profile_id: str, token_json: str) -> Path:
        parsed = json.loads(token_json)
        if not isinstance(parsed, dict):
            raise TypeError("OAuth token must be a JSON object")
        path = self.path_for(profile_id)
        _atomic_private_write(path, json.dumps(parsed, sort_keys=True))
        return path

    def exists(self, profile_id: str) -> bool:
        return self.path_for(profile_id).exists()


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

    def get_cursor(self, profile_id: str) -> str | None:
        cursor = self._read(profile_id).get("cursor")
        return None if cursor is None else str(cursor)

    def set_cursor(self, profile_id: str, cursor: str) -> None:
        state = self._read(profile_id)
        state["cursor"] = cursor
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
        roots = set(root_folder_ids)
        changed = 0
        for file_id, item in state.get("items", {}).items():
            if (
                item.get("root_folder_id") in roots
                and file_id not in seen_file_ids
                and item.get("status") != "removed"
            ):
                item["status"] = "removed"
                changed += 1
        if changed:
            self._write(profile_id, state)
        return changed

    def count_seen(self, profile_id: str) -> int:
        items = self._read(profile_id).get("items", {}).values()
        return sum(item.get("status") == "imported" for item in items)
