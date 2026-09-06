"""Separate private profile and credential stores."""

from __future__ import annotations

import fcntl
import json
import os
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

from health_agent.automation.storage import (
    atomic_private_write,
    private_directory,
    reject_symlink_components,
)
from health_agent.google_calendar.models import CalendarProfile


def _dir(root: Path, profile_id: UUID) -> Path:
    try:
        reject_symlink_components(root)
        private_directory(root)
        return private_directory(root / str(UUID(str(profile_id))))
    except RuntimeError as error:
        raise RuntimeError("refusing symlinked calendar path") from error


def _read(path: Path) -> dict[str, Any]:
    try:
        reject_symlink_components(path)
    except RuntimeError as error:
        raise RuntimeError("refusing symlinked calendar file") from error
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600:
        raise RuntimeError("calendar file must be regular 0600")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise TypeError("invalid_calendar_state")
    return value


@contextmanager
def _lock(root: Path) -> Iterator[None]:
    private_directory(root)
    path = root / "publish.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class CalendarProfileStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, profile_id: UUID) -> Path:
        return _dir(self.root, profile_id) / "profile.json"

    def save(self, profile: CalendarProfile) -> None:
        with _lock(self.root):
            path = self.path_for(profile.profile_id)
            if path.exists():
                existing = _read(path).get("account_subject")
                if existing is not None and existing != profile.account_subject:
                    raise ValueError("calendar profile is bound to a different account")
            atomic_private_write(
                path,
                json.dumps(
                    {
                        "profile_id": str(profile.profile_id),
                        "calendar_id": profile.calendar_id,
                        "account_subject": profile.account_subject,
                        "account_email": profile.account_email,
                        "enabled": profile.enabled,
                    },
                    sort_keys=True,
                ).encode(),
            )

    def load(self, profile_id: UUID) -> CalendarProfile:
        value = _read(self.path_for(profile_id))
        if value.get("profile_id") != str(profile_id):
            raise ValueError("stored calendar profile belongs to another profile")
        return CalendarProfile(
            UUID(value["profile_id"]),
            value["calendar_id"],
            value.get("account_subject"),
            value.get("account_email"),
            bool(value.get("enabled")),
        )


class CalendarTokenStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def path_for(self, profile_id: UUID) -> Path:
        return _dir(self.root, profile_id) / "token.json"

    def publish_verified(
        self, profile_id: UUID, subject: str, email: str, credentials: dict[str, Any]
    ) -> None:
        subject, email = subject.strip(), email.strip().casefold()
        if not subject or "@" not in email or not isinstance(credentials, dict):
            raise ValueError("invalid_verified_identity")
        with _lock(self.root):
            own = self.path_for(profile_id)
            if own.exists() and _read(own).get("account_subject") != subject:
                raise ValueError("profile is bound to a different account")
            for candidate in self.root.glob("*/token.json"):
                value = _read(candidate)
                if value.get("account_subject") == subject and value.get(
                    "profile_id"
                ) != str(profile_id):
                    raise ValueError("Google subject belongs to another profile")
            atomic_private_write(
                own,
                json.dumps(
                    {
                        "profile_id": str(profile_id),
                        "account_subject": subject,
                        "account_email": email,
                        "credentials": credentials,
                    },
                    sort_keys=True,
                ).encode(),
            )

    def load_verified(self, profile_id: UUID) -> dict[str, Any] | None:
        path = self.path_for(profile_id)
        if not path.exists():
            return None
        value = _read(path)
        if value.get("profile_id") != str(profile_id):
            raise ValueError("stored calendar token belongs to another profile")
        if not isinstance(value.get("credentials"), dict):
            raise TypeError("invalid_calendar_token")
        return value
