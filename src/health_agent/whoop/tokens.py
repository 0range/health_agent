from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_KEY = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class TokenStoreError(RuntimeError):
    """A safe token-store error that never includes token contents."""


@dataclass(frozen=True, slots=True, repr=False)
class WhoopToken:
    access_token: str
    refresh_token: str
    expires_at: datetime
    scopes: tuple[str, ...]
    token_type: str = "bearer"

    def __repr__(self) -> str:
        return (
            "WhoopToken(access_token=<redacted>, refresh_token=<redacted>, "
            f"expires_at={self.expires_at!r}, scopes={self.scopes!r}, "
            f"token_type={self.token_type!r})"
        )

    @property
    def expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)

    def as_json(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at.astimezone(UTC).isoformat(),
            "scopes": list(self.scopes),
            "token_type": self.token_type,
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> WhoopToken:
        try:
            expires_at = datetime.fromisoformat(str(value["expires_at"]))
            if expires_at.tzinfo is None:
                raise ValueError
            access_token = str(value["access_token"])
            refresh_token = str(value["refresh_token"])
            scopes = tuple(str(scope) for scope in value.get("scopes", ()))
            token_type = str(value.get("token_type", "bearer"))
        except (KeyError, TypeError, ValueError) as error:
            raise TokenStoreError("WHOOP token file is invalid") from error
        if not access_token or not refresh_token:
            raise TokenStoreError("WHOOP token file is invalid")
        return cls(access_token, refresh_token, expires_at, scopes, token_type)


class TokenStore:
    """Atomic, per-profile WHOOP token files for a local installation."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def _validate_key(value: str, label: str) -> str:
        if _SAFE_KEY.fullmatch(value) is None:
            raise TokenStoreError(
                f"Invalid {label}; use lowercase letters, digits, _ or -"
            )
        return value

    def _path(self, profile_slug: str, account_name: str) -> Path:
        profile = self._validate_key(profile_slug, "profile")
        account = self._validate_key(account_name, "account")
        return self.root / profile / f"{account}.json"

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TokenStoreError("WHOOP token directory is not a regular directory")
        path.chmod(0o700)

    def save(self, profile_slug: str, account_name: str, token: WhoopToken) -> Path:
        target = self._path(profile_slug, account_name)
        self._secure_directory(self.root)
        self._secure_directory(target.parent)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise TokenStoreError("WHOOP token path is not a regular file")

        encoded = json.dumps(token.as_json(), sort_keys=True).encode("utf-8")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, delete=False
            ) as temporary:
                temporary_name = temporary.name
                os.chmod(temporary.fileno(), 0o600)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
            target.chmod(0o600)
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)
        return target

    def load(self, profile_slug: str, account_name: str) -> WhoopToken | None:
        target = self._path(profile_slug, account_name)
        if not target.exists():
            return None
        file_stat = target.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise TokenStoreError("WHOOP token path is not a regular file")
        target.chmod(0o600)
        try:
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TokenStoreError("WHOOP token file cannot be read") from error
        if not isinstance(payload, dict):
            raise TokenStoreError("WHOOP token file is invalid")
        return WhoopToken.from_json(payload)
