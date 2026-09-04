from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

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


@dataclass(frozen=True, slots=True)
class _TokenJournal:
    mode: str
    generation: str | None
    previous: WhoopToken | None
    candidate: WhoopToken

    def as_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "mode": self.mode,
            "generation": self.generation,
            "previous": self.previous.as_json() if self.previous else None,
            "candidate": self.candidate.as_json(),
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> _TokenJournal:
        try:
            if value["version"] != 1 or value["mode"] not in {
                "standalone",
                "coordinated",
            }:
                raise ValueError
            generation_value = value.get("generation")
            generation = str(generation_value) if generation_value is not None else None
            previous_value = value.get("previous")
            previous = (
                WhoopToken.from_json(previous_value)
                if isinstance(previous_value, dict)
                else None
            )
            candidate_value = value["candidate"]
            if not isinstance(candidate_value, dict):
                raise TypeError
            candidate = WhoopToken.from_json(candidate_value)
        except (KeyError, TypeError, ValueError) as error:
            raise TokenStoreError("WHOOP token journal is invalid") from error
        if (value["mode"] == "coordinated") != (generation is not None):
            raise TokenStoreError("WHOOP token journal is invalid")
        return cls(str(value["mode"]), generation, previous, candidate)


class TokenStore:
    """Durable, per-profile WHOOP token files for a local installation.

    All auth, sync, and status entry points acquire ``operation()`` before any
    token-file or database lock. The inner token lock protects file revisions.
    """

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

    def _journal_path(self, profile_slug: str, account_name: str) -> Path:
        return self._path(profile_slug, account_name).with_suffix(".journal")

    def validate_target(self, profile_slug: str, account_name: str) -> Path:
        """Validate a token identity before starting an irreversible OAuth flow."""
        return self._path(profile_slug, account_name)

    @staticmethod
    def _secure_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TokenStoreError("WHOOP token directory is not a regular directory")
        path.chmod(0o700)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        directory_fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)

    def _atomic_json_write(self, target: Path, value: dict[str, Any]) -> None:
        self._secure_directory(self.root)
        self._secure_directory(target.parent)
        if target.exists() and (target.is_symlink() or not target.is_file()):
            raise TokenStoreError("WHOOP token path is not a regular file")
        encoded = json.dumps(value, sort_keys=True).encode("utf-8")
        temporary_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent, delete=False
            ) as temporary:
                temporary_name = temporary.name
                os.fchmod(temporary.fileno(), 0o600)
                temporary.write(encoded)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_name, target)
            temporary_name = None
            self._sync_directory(target.parent)
        except OSError as error:
            raise TokenStoreError("WHOOP token storage is unavailable") from error
        finally:
            if temporary_name is not None:
                Path(temporary_name).unlink(missing_ok=True)

    def _save_token_unlocked(
        self, profile: str, account: str, token: WhoopToken
    ) -> Path:
        target = self._path(profile, account)
        self._atomic_json_write(target, token.as_json())
        return target

    def _write_journal_unlocked(
        self, profile: str, account: str, journal: _TokenJournal
    ) -> None:
        self._atomic_json_write(self._journal_path(profile, account), journal.as_json())

    def _read_journal_unlocked(
        self, profile: str, account: str
    ) -> _TokenJournal | None:
        path = self._journal_path(profile, account)
        if not path.exists():
            return None
        payload = self._read_json_file(path, "WHOOP token journal")
        return _TokenJournal.from_json(payload)

    def _clear_journal_unlocked(self, profile: str, account: str) -> None:
        path = self._journal_path(profile, account)
        if not path.exists():
            return
        try:
            path.unlink()
            self._sync_directory(path.parent)
        except OSError as error:
            raise TokenStoreError("WHOOP token journal cannot be cleared") from error

    @contextmanager
    def _file_lock(
        self,
        profile_slug: str,
        account_name: str,
        *,
        suffix: str,
        exclusive: bool,
    ) -> Iterator[None]:
        target = self._path(profile_slug, account_name)
        self._secure_directory(self.root)
        self._secure_directory(target.parent)
        lock_path = target.with_suffix(suffix)
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            lock_fd = os.open(lock_path, flags, 0o600)
        except OSError as error:
            raise TokenStoreError("WHOOP token lock cannot be opened") from error
        try:
            os.fchmod(lock_fd, 0o600)
            if not stat.S_ISREG(os.fstat(lock_fd).st_mode):
                raise TokenStoreError("WHOOP token lock is not a regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    @contextmanager
    def operation(self, profile_slug: str, account_name: str) -> Iterator[None]:
        """Acquire the outermost per-account lock required by every public flow."""
        with self._file_lock(
            profile_slug,
            account_name,
            suffix=".operation.lock",
            exclusive=True,
        ):
            yield

    @contextmanager
    def _locked(
        self, profile_slug: str, account_name: str, *, exclusive: bool
    ) -> Iterator[None]:
        with self._file_lock(
            profile_slug, account_name, suffix=".lock", exclusive=exclusive
        ):
            yield

    def _save_with_journal_unlocked(
        self, profile: str, account: str, token: WhoopToken
    ) -> Path:
        self._recover_unlocked(profile, account, committed_generation=None)
        previous = self._load_token_unlocked(profile, account)
        self._write_journal_unlocked(
            profile, account, _TokenJournal("standalone", None, previous, token)
        )
        target = self._save_token_unlocked(profile, account, token)
        self._clear_journal_unlocked(profile, account)
        return target

    def save(self, profile_slug: str, account_name: str, token: WhoopToken) -> Path:
        with self._locked(profile_slug, account_name, exclusive=True):
            return self._save_with_journal_unlocked(profile_slug, account_name, token)

    def rotate(
        self,
        profile_slug: str,
        account_name: str,
        stale_refresh_token: str,
        refresher: Callable[[str], WhoopToken],
    ) -> WhoopToken:
        """Rotate once even when multiple processes observe the same stale token."""
        with self._locked(profile_slug, account_name, exclusive=True):
            self._recover_unlocked(
                profile_slug, account_name, committed_generation=None
            )
            current = self._load_token_unlocked(profile_slug, account_name)
            if current is None:
                raise TokenStoreError("WHOOP account is not authorized")
            if current.refresh_token != stale_refresh_token and not current.expired:
                return current
            refreshed = refresher(current.refresh_token)
            self._save_with_journal_unlocked(profile_slug, account_name, refreshed)
            return refreshed

    @contextmanager
    def replacement(
        self,
        profile_slug: str,
        account_name: str,
        token: WhoopToken,
    ) -> Iterator[TokenReplacement]:
        """Create a DB-coordinated replacement journal under the token lock."""
        with self._locked(profile_slug, account_name, exclusive=True):
            self._recover_unlocked(
                profile_slug, account_name, committed_generation=None
            )
            previous = self._load_token_unlocked(profile_slug, account_name)
            generation = uuid4()
            self._write_journal_unlocked(
                profile_slug,
                account_name,
                _TokenJournal("coordinated", str(generation), previous, token),
            )
            replacement = TokenReplacement(
                self, profile_slug, account_name, generation, token
            )
            completed_normally = False
            try:
                yield replacement
                completed_normally = True
            finally:
                # On an exception the database may already have committed this
                # generation, so keep the journal for authoritative recovery.
                if completed_normally and not replacement.resolved:
                    replacement.rollback()

    def recover(
        self,
        profile_slug: str,
        account_name: str,
        committed_generation: UUID | None,
    ) -> None:
        """Reconcile an interrupted replacement against committed database state."""
        with self._locked(profile_slug, account_name, exclusive=True):
            self._recover_unlocked(
                profile_slug,
                account_name,
                committed_generation=(
                    str(committed_generation) if committed_generation else None
                ),
                allow_coordinated=True,
            )

    def _recover_unlocked(
        self,
        profile: str,
        account: str,
        committed_generation: str | None,
        *,
        allow_coordinated: bool = False,
    ) -> None:
        journal = self._read_journal_unlocked(profile, account)
        if journal is None:
            return
        if journal.mode == "coordinated" and not allow_coordinated:
            raise TokenStoreError("WHOOP token requires database reconciliation")
        keep_candidate = journal.mode == "standalone" or (
            journal.generation == committed_generation
        )
        selected = journal.candidate if keep_candidate else journal.previous
        if selected is None:
            self._delete_token_unlocked(profile, account)
        else:
            self._save_token_unlocked(profile, account, selected)
        self._clear_journal_unlocked(profile, account)

    def _delete_token_unlocked(self, profile: str, account: str) -> None:
        target = self._path(profile, account)
        if not target.exists():
            return
        try:
            target.unlink()
            self._sync_directory(target.parent)
        except OSError as error:
            raise TokenStoreError("WHOOP token cannot be restored") from error

    def _read_json_file(self, target: Path, label: str) -> dict[str, Any]:
        for directory in (self.root, target.parent):
            directory_stat = directory.lstat()
            if stat.S_ISLNK(directory_stat.st_mode) or not stat.S_ISDIR(
                directory_stat.st_mode
            ):
                raise TokenStoreError(
                    "WHOOP token directory is not a regular directory"
                )
        file_stat = target.lstat()
        if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
            raise TokenStoreError(f"{label} path is not a regular file")
        try:
            target.chmod(0o600)
            payload = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise TokenStoreError(f"{label} cannot be read") from error
        if not isinstance(payload, dict):
            raise TokenStoreError(f"{label} is invalid")
        return payload

    def _load_token_unlocked(
        self, profile_slug: str, account_name: str
    ) -> WhoopToken | None:
        target = self._path(profile_slug, account_name)
        if not target.exists():
            return None
        return WhoopToken.from_json(self._read_json_file(target, "WHOOP token file"))

    def load(self, profile_slug: str, account_name: str) -> WhoopToken | None:
        with self._locked(profile_slug, account_name, exclusive=True):
            self._recover_unlocked(
                profile_slug, account_name, committed_generation=None
            )
            return self._load_token_unlocked(profile_slug, account_name)


class TokenReplacement:
    """A durable replacement held under the account's exclusive token lock."""

    def __init__(
        self,
        store: TokenStore,
        profile_slug: str,
        account_name: str,
        generation: UUID,
        candidate: WhoopToken,
    ) -> None:
        self._store = store
        self._profile_slug = profile_slug
        self._account_name = account_name
        self._generation = generation
        self._candidate = candidate
        self._published = False
        self._resolved = False

    @property
    def generation(self) -> UUID:
        return self._generation

    @property
    def resolved(self) -> bool:
        return self._resolved

    def publish(self) -> Path:
        self._published = True
        return self._store._save_token_unlocked(
            self._profile_slug, self._account_name, self._candidate
        )

    def resolve(self, committed_generation: UUID | None) -> None:
        """Idempotently select the token matching committed database state."""
        if not self._published:
            raise TokenStoreError("WHOOP token replacement was not published")
        self._store._recover_unlocked(
            self._profile_slug,
            self._account_name,
            str(committed_generation) if committed_generation else None,
            allow_coordinated=True,
        )
        self._resolved = True

    def rollback(self) -> None:
        journal = self._store._read_journal_unlocked(
            self._profile_slug, self._account_name
        )
        if journal is None:
            return
        if journal.previous is None:
            self._store._delete_token_unlocked(self._profile_slug, self._account_name)
        else:
            self._store._save_token_unlocked(
                self._profile_slug, self._account_name, journal.previous
            )
        self._store._clear_journal_unlocked(self._profile_slug, self._account_name)
