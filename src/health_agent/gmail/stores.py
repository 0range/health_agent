"""Private atomic local stores for Gmail profiles, tokens, and cursors."""

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

from health_agent.gmail.config import (
    GmailProfile,
    normalize_profile_id,
    validate_account_id,
)
from health_agent.gmail.types import GmailRunState, SeenAttachment, SeenMessage


class GmailBindingConflict(ValueError):
    """A mailbox is already bound to another health profile."""


def _private_directory(path: Path) -> Path:
    _reject_symlink_components(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError(f"refusing non-directory Gmail path: {path}")
    path.chmod(0o700)
    return path


def _reject_symlink_components(path: Path) -> None:
    absolute = path if path.is_absolute() else Path.cwd() / path
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise RuntimeError(f"refusing symlinked Gmail directory: {component}")


def _profile_directory(root: Path, profile_id: str) -> Path:
    return _private_directory(Path(root) / normalize_profile_id(profile_id))


def _account_directory(root: Path, profile_id: str, account_id: str) -> Path:
    accounts = _private_directory(_profile_directory(root, profile_id) / "accounts")
    return _private_directory(accounts / validate_account_id(account_id))


def _atomic_private_write(path: Path, content: str) -> None:
    _private_directory(path.parent)
    if path.is_symlink():
        raise RuntimeError(f"refusing symlinked Gmail state file: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
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
        raise RuntimeError(f"refusing non-regular Gmail state file: {path}")
    if not path.exists():
        return default
    if not path.is_file():
        raise RuntimeError(f"refusing non-regular Gmail state file: {path}")
    path.chmod(0o600)
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Gmail state must be a JSON object: {path}")
    return value


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
        path = self.path_for(profile_id, account_id)
        if path.is_symlink():
            raise RuntimeError("refusing symlinked Gmail OAuth token")
        return path.is_file()

    def publish_verified(
        self,
        profile_id: str,
        account_id: str,
        bound_email: str,
        token_json: str,
    ) -> Path:
        profile_id = normalize_profile_id(profile_id)
        account_id = validate_account_id(account_id)
        credentials = json.loads(token_json)
        if not isinstance(credentials, dict):
            raise TypeError("OAuth token must be a JSON object")
        email = bound_email.strip().casefold()
        if "@" not in email or any(char.isspace() for char in email):
            raise ValueError("verified Gmail identity is invalid")
        root = _private_directory(self.root)
        with _exclusive_file_lock(root / "authorizations.lock"):
            for path in root.glob("*/accounts/*/token.json"):
                if path.is_symlink():
                    raise RuntimeError("refusing symlinked Gmail OAuth token")
                existing = _read_json(path, {})
                if (
                    existing.get("bound_email") == email
                    and existing.get("profile_id") != profile_id
                ):
                    raise GmailBindingConflict(
                        "Gmail account is already bound to another health profile"
                    )
            path = self.path_for(profile_id, account_id)
            payload = {
                "profile_id": profile_id,
                "account_id": account_id,
                "bound_email": email,
                "credentials": credentials,
            }
            _atomic_private_write(path, json.dumps(payload, sort_keys=True))
            return path

    def load_verified(
        self, profile_id: str, account_id: str
    ) -> tuple[str, dict[str, Any]] | None:
        path = self.path_for(profile_id, account_id)
        if path.is_symlink():
            raise RuntimeError("refusing symlinked Gmail OAuth token")
        if not path.exists():
            return None
        payload = _read_json(path, {})
        expected_profile = normalize_profile_id(profile_id)
        expected_account = validate_account_id(account_id)
        if payload.get("profile_id") != expected_profile:
            raise ValueError("stored Gmail token belongs to another health profile")
        if payload.get("account_id") != expected_account:
            raise ValueError("stored Gmail token belongs to another account slot")
        email = payload.get("bound_email")
        credentials = payload.get("credentials")
        if not isinstance(email, str) or not isinstance(credentials, dict):
            raise TypeError("stored Gmail authorization is incomplete")
        return email, credentials

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
            {
                "history_id": None,
                "messages": {},
                "attachments": {},
                "run": {},
            },
        )

    def _write(self, profile_id: str, account_id: str, value: dict[str, Any]) -> None:
        _atomic_private_write(
            self._path(profile_id, account_id),
            json.dumps(value, sort_keys=True, indent=2),
        )

    def get_cursor(self, profile_id: str, account_id: str) -> str | None:
        value = self._read(profile_id, account_id).get("history_id")
        return None if value is None else str(value)

    def sync_lock(
        self, profile_id: str, account_id: str
    ) -> AbstractContextManager[None]:
        return _exclusive_file_lock(
            _account_directory(self.root, profile_id, account_id) / "sync.lock"
        )

    def begin_sync(self, profile_id: str, account_id: str, mode: str) -> None:
        value = self._read(profile_id, account_id)
        run = value.setdefault("run", {})
        run["last_attempt_at"] = _now()
        run["last_mode"] = mode
        run["last_error_code"] = None
        self._write(profile_id, account_id, value)

    def finish_sync(self, profile_id: str, account_id: str) -> None:
        value = self._read(profile_id, account_id)
        run = value.setdefault("run", {})
        run["last_success_at"] = _now()
        run["last_error_code"] = None
        self._write(profile_id, account_id, value)

    def fail_sync(self, profile_id: str, account_id: str, safe_error_code: str) -> None:
        value = self._read(profile_id, account_id)
        run = value.setdefault("run", {})
        run["last_attempt_at"] = run.get("last_attempt_at") or _now()
        run["last_error_code"] = safe_error_code
        self._write(profile_id, account_id, value)

    def get_run_state(self, profile_id: str, account_id: str) -> GmailRunState:
        run = self._read(profile_id, account_id).get("run", {})
        return GmailRunState(
            last_attempt_at=run.get("last_attempt_at"),
            last_success_at=run.get("last_success_at"),
            last_error_code=run.get("last_error_code"),
            last_mode=run.get("last_mode"),
        )

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
        value["label_ids"] = tuple(value.get("label_ids", ()))
        return SeenMessage(**value)

    def record_message(self, message: SeenMessage) -> None:
        value = self._read(message.profile_id, message.account_id)
        value.setdefault("messages", {})[message.message_id] = asdict(message)
        self._write(message.profile_id, message.account_id, value)

    def get_attachment(
        self,
        profile_id: str,
        account_id: str,
        message_id: str,
        part_id: str,
        revision: str,
    ) -> SeenAttachment | None:
        key = _attachment_key(message_id, part_id, revision)
        value = self._read(profile_id, account_id).get("attachments", {}).get(key)
        if value is None:
            return None
        self._require_boundary(value, profile_id, account_id)
        return SeenAttachment(**value)

    def record_attachment(self, attachment: SeenAttachment) -> None:
        value = self._read(attachment.profile_id, attachment.account_id)
        key = _attachment_key(
            attachment.message_id, attachment.part_id, attachment.revision
        )
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

    def counts(self, profile_id: str, account_id: str) -> dict[str, int]:
        value = self._read(profile_id, account_id)
        messages = sum(
            item.get("status") != "removed"
            for item in value.get("messages", {}).values()
        )
        attention_messages = sum(
            item.get("status") == "attention"
            for item in value.get("messages", {}).values()
        )
        counts: dict[str, int] = {
            "messages": messages,
            "attention_messages": attention_messages,
            "attention_attachments": 0,
            "staged": 0,
        }
        for item in value.get("attachments", {}).values():
            outcome = item.get("outcome") or item.get("status")
            counts[outcome] = counts.get(outcome, 0) + 1
            if item.get("status") == "attention":
                counts["attention_attachments"] += 1
            if item.get("storage_reference") is not None:
                counts["staged"] += 1
        return counts

    def attention_items(
        self, profile_id: str, account_id: str
    ) -> tuple[SeenAttachment, ...]:
        value = self._read(profile_id, account_id)
        items: list[SeenAttachment] = []
        for raw in value.get("attachments", {}).values():
            if raw.get("status") != "attention":
                continue
            self._require_boundary(raw, profile_id, account_id)
            items.append(SeenAttachment(**raw))
        return tuple(items)

    @staticmethod
    def _require_boundary(
        value: dict[str, Any], profile_id: str, account_id: str
    ) -> None:
        if value.get("profile_id") != normalize_profile_id(profile_id):
            raise ValueError("stored Gmail state belongs to another profile")
        if value.get("account_id") != validate_account_id(account_id):
            raise ValueError("stored Gmail state belongs to another account")


def _attachment_key(message_id: str, part_id: str, revision: str) -> str:
    return f"{message_id}:{part_id}:{revision}"


def _now() -> str:
    return datetime.now(UTC).isoformat()
