"""Profile configuration and user-supplied Drive folder validation."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit

DRIVE_READONLY_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]{10,200}")
_DRIVE_HOSTS = {"drive.google.com", "docs.google.com"}


def validate_profile_id(value: str) -> str:
    """Return a safe local profile key or raise a user-facing error."""
    if _PROFILE_ID.fullmatch(value) is None:
        raise ValueError(
            "profile ID must be 1-64 letters, digits, underscores, or hyphens"
        )
    return value


def normalize_folder_id(value: str) -> str:
    """Accept a Drive folder ID or canonical folder URL and return its opaque ID."""
    candidate = value.strip()
    if _DRIVE_ID.fullmatch(candidate):
        return candidate

    parsed = urlsplit(candidate)
    if parsed.scheme != "https" or parsed.hostname not in _DRIVE_HOSTS:
        raise ValueError("folder must be a Google Drive HTTPS folder URL or folder ID")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("folder URL must not contain credentials or a fragment")

    parts = [part for part in parsed.path.split("/") if part]
    folder_id: str | None = None
    if len(parts) >= 3 and parts[-2] == "folders":
        folder_id = parts[-1]
    elif parsed.hostname == "drive.google.com" and parsed.path == "/open":
        values = parse_qs(parsed.query).get("id", [])
        if len(values) == 1:
            folder_id = values[0]
    if folder_id is None or _DRIVE_ID.fullmatch(folder_id) is None:
        raise ValueError("URL does not identify a Google Drive folder")
    return folder_id


@dataclass(frozen=True, slots=True)
class DriveProfile:
    """Connector configuration for exactly one local person and Google account."""

    profile_id: str
    root_folder_ids: tuple[str, ...]
    account_email: str | None = None

    @classmethod
    def create(
        cls, profile_id: str, folders: list[str] | tuple[str, ...]
    ) -> DriveProfile:
        normalized = tuple(dict.fromkeys(normalize_folder_id(value) for value in folders))
        if not normalized:
            raise ValueError("at least one Google Drive folder is required")
        return cls(validate_profile_id(profile_id), normalized)

    def with_account(self, email: str) -> DriveProfile:
        email = email.strip().casefold()
        if "@" not in email or any(character.isspace() for character in email):
            raise ValueError("Google account email is invalid")
        return DriveProfile(self.profile_id, self.root_folder_ids, email)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["root_folder_ids"] = list(self.root_folder_ids)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DriveProfile:
        profile_id = validate_profile_id(str(data["profile_id"]))
        raw_roots = data["root_folder_ids"]
        if not isinstance(raw_roots, list):
            raise TypeError("root_folder_ids must be a list")
        profile = cls.create(profile_id, [str(value) for value in raw_roots])
        account = data.get("account_email")
        return profile if account is None else profile.with_account(str(account))
