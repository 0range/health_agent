"""Private atomic local stores for Gmail profiles, tokens, and cursors."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any

from health_agent.gmail.config import (
    GmailProfile,
    normalize_profile_id,
    validate_account_id,
)
from health_agent.gmail.types import SeenAttachment, SeenMessage


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _profile_directory(root: Path, profile_id: str) -> Path:
    return _private_directory(Path(root) / normalize_profile_id(profile_id))


def _account_directory(root: Path, profile_id: str, account_id: str) -> Path:
    accounts = _private_directory(_profile_directory(root, profile_id) / "accounts")
    return _private_directory(accounts / validate_account_id(account_id))


def _atomic_private_write(path: Path, content: str) -> None:
    _private_directory(path.parent)
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
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"refusing non-regular Gmail state file: {path}")
    path.chmod(0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Gmail state must be a JSON object: {path}")
    return value


class LocalGmailProfileStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, profile_id: str) -> Path:
        return _profile_directory(self.root, profile_id) / "profile.json"

    def exists(self, profile_id: str) -> bool:
        return self._path(profile_id).exists()

    def load(self, profile_id: str) -> GmailProfile:
        path = self._path(profile_id)
        if not path.exists():
            raise FileNotFoundError(f"Gmail profile {profile_id!r} is not configured")
        profile = GmailProfile.from_dict(_read_json(path, {}))
        if profile.profile_id != normalize_profile_id(profile_id):
            raise ValueError("stored Gmail profile belongs to another profile")
        return profile

    def save(self, profile: GmailProfile) -> None:
        _atomic_private_write(
            self._path(profile.profile_id),
            json.dumps(profile.to_dict(), sort_keys=True, indent=2),
        )


class LocalGmailTokenStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def path_for(self, profile_id: str, account_id: str) -> Path:
        return _account_directory(self.root, profile_id, account_id) / "token.json"

    def exists(self, profile_id: str, account_id: str) -> bool:
        return self.path_for(profile_id, account_id).exists()

    def save(self, profile_id: str, account_id: str, token_json: str) -> Path:
        value = json.loads(token_json)
        if not isinstance(value, dict):
            raise TypeError("OAuth token must be a JSON object")
        path = self.path_for(profile_id, account_id)
        _atomic_private_write(path, json.dumps(value, sort_keys=True))
        return path

    def clear(self, profile_id: str, account_id: str) -> None:
        path = self.path_for(profile_id, account_id)
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError("refusing to clear non-regular Gmail OAuth token")
        path.unlink(missing_ok=True)


class LocalGmailStateStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def _path(self, profile_id: str, account_id: str) -> Path:
        return _account_directory(self.root, profile_id, account_id) / "sync-state.json"

    def _read(self, profile_id: str, account_id: str) -> dict[str, Any]:
        return _read_json(
            self._path(profile_id, account_id),
            {"history_id": None, "messages": {}, "attachments": {}},
        )

    def _write(self, profile_id: str, account_id: str, value: dict[str, Any]) -> None:
        _atomic_private_write(
            self._path(profile_id, account_id),
            json.dumps(value, sort_keys=True, indent=2),
        )

    def get_cursor(self, profile_id: str, account_id: str) -> str | None:
        value = self._read(profile_id, account_id).get("history_id")
        return None if value is None else str(value)

    def set_cursor(self, profile_id: str, account_id: str, history_id: str) -> None:
        value = self._read(profile_id, account_id)
        value["history_id"] = history_id
        self._write(profile_id, account_id, value)

    def get_message(
        self, profile_id: str, account_id: str, message_id: str
    ) -> SeenMessage | None:
        value = self._read(profile_id, account_id).get("messages", {}).get(message_id)
        if value is None:
            return None
        self._require_boundary(value, profile_id, account_id)
        return SeenMessage(**value)

    def record_message(self, message: SeenMessage) -> None:
        value = self._read(message.profile_id, message.account_id)
        value.setdefault("messages", {})[message.message_id] = asdict(message)
        self._write(message.profile_id, message.account_id, value)

    def get_attachment(
        self, profile_id: str, account_id: str, message_id: str, part_id: str
    ) -> SeenAttachment | None:
        key = _attachment_key(message_id, part_id)
        value = self._read(profile_id, account_id).get("attachments", {}).get(key)
        if value is None:
            return None
        self._require_boundary(value, profile_id, account_id)
        return SeenAttachment(**value)

    def record_attachment(self, attachment: SeenAttachment) -> None:
        value = self._read(attachment.profile_id, attachment.account_id)
        key = _attachment_key(attachment.message_id, attachment.part_id)
        value.setdefault("attachments", {})[key] = asdict(attachment)
        self._write(attachment.profile_id, attachment.account_id, value)

    def mark_message_removed(
        self, profile_id: str, account_id: str, message_id: str
    ) -> int:
        value = self._read(profile_id, account_id)
        changed = 0
        message = value.get("messages", {}).get(message_id)
        if message is not None and message.get("status") != "removed":
            message["status"] = "removed"
            changed += 1
        prefix = f"{message_id}:"
        for key, attachment in value.get("attachments", {}).items():
            if key.startswith(prefix) and attachment.get("status") != "removed":
                attachment["status"] = "removed"
                changed += 1
        if changed:
            self._write(profile_id, account_id, value)
        return changed

    def counts(self, profile_id: str, account_id: str) -> tuple[int, int, int]:
        value = self._read(profile_id, account_id)
        messages = sum(
            item.get("status") != "removed" for item in value.get("messages", {}).values()
        )
        imported = sum(
            item.get("status") == "imported"
            for item in value.get("attachments", {}).values()
        )
        ambiguous = sum(
            item.get("status") == "ambiguous"
            for item in value.get("attachments", {}).values()
        )
        return messages, imported, ambiguous

    @staticmethod
    def _require_boundary(
        value: dict[str, Any], profile_id: str, account_id: str
    ) -> None:
        if value.get("profile_id") != normalize_profile_id(profile_id):
            raise ValueError("stored Gmail state belongs to another profile")
        if value.get("account_id") != validate_account_id(account_id):
            raise ValueError("stored Gmail state belongs to another account")


def _attachment_key(message_id: str, part_id: str) -> str:
    return f"{message_id}:{part_id}"
