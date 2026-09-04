"""Symlink-safe credentials and bot-scoped, content-free Telegram state."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from health_agent.telegram.types import (
    ClaimResult,
    InboxReceipt,
    OutboundReservation,
    TelegramIdentity,
    UpdateClaim,
    VerifiedBotCredential,
)

LEGACY_BOT_ID = 0
STATE_SCHEMA_VERSION = 2


class TelegramIdentityConflict(ValueError):
    """The requested user/profile binding conflicts with committed ownership."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("Telegram state timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _absolute_without_resolving(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _reject_symlink_components(path: Path, *, include_final: bool) -> None:
    absolute = _absolute_without_resolving(path)
    parts = absolute.parts
    current = Path(parts[0])
    stop = len(parts) - 1
    for part in parts[1:stop]:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(mode):
            raise ValueError(f"Private path contains a symlink: {current}")
        if not stat.S_ISDIR(mode):
            raise ValueError(f"Private path component is not a directory: {current}")
    if include_final and len(parts) > 1:
        current = absolute
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            return
        if stat.S_ISLNK(mode):
            raise ValueError(f"Private path is a symlink: {current}")


def private_directory(path: Path) -> Path:
    path = Path(path)
    _reject_symlink_components(path, include_final=True)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    _reject_symlink_components(path, include_final=True)
    if not path.is_dir():
        raise ValueError(f"Private path is not a directory: {path}")
    path.chmod(0o700)
    return path


def _reject_non_regular_target(path: Path) -> None:
    _reject_symlink_components(path, include_final=True)
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise ValueError(f"Private path is not a regular file: {path}")


def _atomic_private_write(path: Path, value: str) -> None:
    path = Path(path)
    private_directory(path.parent)
    _reject_non_regular_target(path)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}."
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        # Refuse a last-moment symlink replacement instead of following it.
        _reject_non_regular_target(path)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


class PrivateBotTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @staticmethod
    def _validate_token(token: str) -> str:
        token = token.strip()
        if (
            not token
            or ":" not in token
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Telegram bot token has an invalid format")
        return token

    def save_verified(self, credential: VerifiedBotCredential) -> None:
        if credential.bot_id <= 0:
            raise ValueError("Telegram bot ID must be positive")
        token = self._validate_token(credential.token)
        payload = json.dumps(
            {
                "version": 1,
                "bot_id": credential.bot_id,
                "username": credential.username,
                "token": token,
            },
            separators=(",", ":"),
        )
        _atomic_private_write(self.path, payload)

    def read_candidate_token(self) -> str:
        """Read a verified JSON credential or a legacy raw token for re-verification."""
        raw = self._read_raw()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return self._validate_token(raw)
        if not isinstance(value, dict) or not isinstance(value.get("token"), str):
            raise ValueError("Stored Telegram credential is invalid")  # noqa: TRY004
        return self._validate_token(value["token"])

    def load_verified(self) -> VerifiedBotCredential:
        raw = self._read_raw()
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError(
                "Stored Telegram credential has not been verified"
            ) from error
        if not isinstance(value, dict) or value.get("version") != 1:
            raise ValueError("Stored Telegram credential is invalid")
        token = value.get("token")
        bot_id = value.get("bot_id")
        username = value.get("username")
        if (
            not isinstance(token, str)
            or isinstance(bot_id, bool)
            or not isinstance(bot_id, int)
            or bot_id <= 0
            or (username is not None and not isinstance(username, str))
        ):
            raise ValueError("Stored Telegram credential is invalid")
        return VerifiedBotCredential(self._validate_token(token), bot_id, username)

    def _read_raw(self) -> str:
        private_directory(self.path.parent)
        _reject_non_regular_target(self.path)
        if not self.path.is_file():
            raise FileNotFoundError("Telegram bot token is not configured")
        self.path.chmod(0o600)
        return self.path.read_text(encoding="utf-8").strip()

    def exists(self) -> bool:
        private_directory(self.path.parent)
        _reject_non_regular_target(self.path)
        return self.path.is_file()


class SqliteTelegramState:
    """Replaceable bot-scoped state; never stores dialogue or file bytes."""

    def __init__(self, path: Path, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self.path = Path(path)
        self._clock = clock
        private_directory(self.path.parent)
        _reject_non_regular_target(self.path)
        self._initialize()

    def _now(self) -> datetime:
        return _as_utc(self._clock())

    def _connect(self) -> sqlite3.Connection:
        private_directory(self.path.parent)
        _reject_non_regular_target(self.path)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        _reject_non_regular_target(self.path)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            columns = self._columns(connection, "identities")
            if columns and "bot_id" not in columns:
                self._migrate_legacy(connection)
            self._create_schema(connection)
            connection.execute(f"PRAGMA user_version = {STATE_SCHEMA_VERSION}")
        self.path.chmod(0o600)

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            str(row["name"])
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    @staticmethod
    def _create_schema(connection: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS bot_namespaces (
                bot_id INTEGER PRIMARY KEY,
                username TEXT,
                verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS identities (
                bot_id INTEGER NOT NULL REFERENCES bot_namespaces(bot_id),
                telegram_user_id INTEGER NOT NULL,
                profile_id TEXT NOT NULL,
                private_chat_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                created_at TEXT NOT NULL,
                PRIMARY KEY (bot_id, telegram_user_id),
                UNIQUE (bot_id, profile_id)
            );
            CREATE TABLE IF NOT EXISTS runtimes (
                bot_id INTEGER PRIMARY KEY REFERENCES bot_namespaces(bot_id),
                next_offset INTEGER,
                last_poll_at TEXT,
                last_error_code TEXT
            );
            CREATE TABLE IF NOT EXISTS updates (
                bot_id INTEGER NOT NULL REFERENCES bot_namespaces(bot_id),
                update_id INTEGER NOT NULL,
                telegram_user_id INTEGER,
                chat_id INTEGER,
                message_id INTEGER,
                profile_id TEXT,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                safe_error_code TEXT,
                received_at TEXT NOT NULL,
                completed_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                next_retry_at TEXT,
                claim_owner TEXT,
                claim_generation INTEGER NOT NULL DEFAULT 0,
                lease_until TEXT,
                PRIMARY KEY (bot_id, update_id)
            );
            CREATE TABLE IF NOT EXISTS attachment_audit (
                bot_id INTEGER NOT NULL,
                update_id INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                status TEXT NOT NULL,
                external_reference TEXT,
                PRIMARY KEY (bot_id, update_id),
                FOREIGN KEY (bot_id, update_id) REFERENCES updates(bot_id, update_id)
            );
            CREATE TABLE IF NOT EXISTS outbound_audit (
                bot_id INTEGER NOT NULL REFERENCES bot_namespaces(bot_id),
                profile_id TEXT NOT NULL,
                delivery_key TEXT NOT NULL,
                part_index INTEGER NOT NULL CHECK (part_index >= 0),
                chat_id INTEGER NOT NULL,
                content_sha256 TEXT,
                status TEXT NOT NULL,
                telegram_message_id INTEGER,
                safe_error_code TEXT,
                next_retry_at TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (bot_id, profile_id, delivery_key, part_index)
            );
            """
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(statement)

    def _migrate_legacy(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            for table in (
                "identities",
                "runtime",
                "updates",
                "attachment_audit",
                "outbound_audit",
            ):
                if self._columns(connection, table):
                    connection.execute(f"ALTER TABLE {table} RENAME TO legacy_{table}")
            self._create_schema(connection)
            now = self._now().isoformat()
            connection.execute(
                "INSERT INTO bot_namespaces(bot_id, username, verified_at) VALUES (0, NULL, ?)",
                (now,),
            )
            if self._columns(connection, "legacy_identities"):
                connection.execute(
                    """INSERT INTO identities
                    SELECT 0, telegram_user_id, profile_id, private_chat_id, active, created_at
                    FROM legacy_identities"""
                )
            if self._columns(connection, "legacy_runtime"):
                connection.execute(
                    """INSERT INTO runtimes(bot_id, next_offset, last_poll_at, last_error_code)
                    SELECT 0, next_offset, last_poll_at, last_error_code FROM legacy_runtime
                    WHERE singleton=1"""
                )
            if self._columns(connection, "legacy_updates"):
                connection.execute(
                    """INSERT INTO updates(
                        bot_id, update_id, telegram_user_id, chat_id, message_id,
                        profile_id, kind, status, safe_error_code, received_at,
                        completed_at, attempt_count, claim_generation
                    ) SELECT 0, update_id, telegram_user_id, chat_id, message_id,
                        profile_id, kind, status, safe_error_code, received_at,
                        completed_at, CASE WHEN status='processing' THEN 1 ELSE 0 END, 0
                    FROM legacy_updates"""
                )
            if self._columns(connection, "legacy_attachment_audit"):
                connection.execute(
                    """INSERT INTO attachment_audit
                    SELECT 0, update_id, sha256, size_bytes, status, external_reference
                    FROM legacy_attachment_audit"""
                )
            if self._columns(connection, "legacy_outbound_audit"):
                connection.execute(
                    """INSERT INTO outbound_audit(
                        bot_id, profile_id, delivery_key, part_index, chat_id,
                        content_sha256, status, telegram_message_id, safe_error_code,
                        next_retry_at, created_at, completed_at
                    ) SELECT 0, profile_id, delivery_key, part_index, chat_id,
                        NULL, status, telegram_message_id, safe_error_code,
                        NULL, created_at, completed_at
                    FROM legacy_outbound_audit"""
                )
            for table in (
                "legacy_attachment_audit",
                "legacy_outbound_audit",
                "legacy_updates",
                "legacy_identities",
                "legacy_runtime",
            ):
                if self._columns(connection, table):
                    connection.execute(f"DROP TABLE {table}")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys = ON")

    def register_bot(self, bot_id: int, username: str | None) -> None:
        if bot_id <= 0:
            raise ValueError("Telegram bot ID must be positive")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """INSERT INTO bot_namespaces(bot_id, username, verified_at)
                VALUES (?, ?, ?) ON CONFLICT(bot_id) DO UPDATE SET
                username=excluded.username, verified_at=excluded.verified_at""",
                (bot_id, username, self._now().isoformat()),
            )
            connection.execute(
                "INSERT OR IGNORE INTO runtimes(bot_id) VALUES (?)", (bot_id,)
            )
            connection.execute("COMMIT")

    def bind_identity(
        self, bot_id: int, identity: TelegramIdentity
    ) -> TelegramIdentity:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                user_row = connection.execute(
                    """SELECT profile_id FROM identities
                    WHERE bot_id=? AND telegram_user_id=? AND active=1""",
                    (bot_id, identity.telegram_user_id),
                ).fetchone()
                profile_row = connection.execute(
                    """SELECT telegram_user_id FROM identities
                    WHERE bot_id=? AND profile_id=? AND active=1""",
                    (bot_id, str(identity.profile_id)),
                ).fetchone()
                if user_row is not None and str(user_row["profile_id"]) != str(
                    identity.profile_id
                ):
                    raise TelegramIdentityConflict("Telegram identity is already bound")
                if (
                    profile_row is not None
                    and int(profile_row["telegram_user_id"])
                    != identity.telegram_user_id
                ):
                    raise TelegramIdentityConflict("Telegram identity is already bound")
                connection.execute(
                    """DELETE FROM identities WHERE bot_id=? AND active=0 AND
                    (telegram_user_id=? OR profile_id=?)""",
                    (bot_id, identity.telegram_user_id, str(identity.profile_id)),
                )
                connection.execute(
                    """INSERT INTO identities(
                        bot_id, telegram_user_id, profile_id, private_chat_id,
                        active, created_at
                    ) VALUES (?, ?, ?, ?, 1, ?)
                    ON CONFLICT(bot_id, telegram_user_id) DO UPDATE SET
                        private_chat_id=excluded.private_chat_id, active=1""",
                    (
                        bot_id,
                        identity.telegram_user_id,
                        str(identity.profile_id),
                        identity.private_chat_id,
                        self._now().isoformat(),
                    ),
                )
                row = connection.execute(
                    """SELECT telegram_user_id, profile_id, private_chat_id, active
                    FROM identities WHERE bot_id=? AND telegram_user_id=?""",
                    (bot_id, identity.telegram_user_id),
                ).fetchone()
                connection.execute("COMMIT")
            except sqlite3.IntegrityError as error:
                connection.execute("ROLLBACK")
                raise TelegramIdentityConflict(
                    "Telegram identity is already bound"
                ) from error
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return self._identity(row)

    def unbind_identity(self, bot_id: int, profile_id: UUID) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE identities SET active=0
                WHERE bot_id=? AND profile_id=? AND active=1""",
                (bot_id, str(profile_id)),
            )
            return cursor.rowcount == 1

    def identity_for_user(
        self, bot_id: int, telegram_user_id: int
    ) -> TelegramIdentity | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT telegram_user_id, profile_id, private_chat_id, active
                FROM identities WHERE bot_id=? AND telegram_user_id=? AND active=1""",
                (bot_id, telegram_user_id),
            ).fetchone()
        return None if row is None else self._identity(row)

    def identity_for_profile(
        self, bot_id: int, profile_id: UUID
    ) -> TelegramIdentity | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT telegram_user_id, profile_id, private_chat_id, active
                FROM identities WHERE bot_id=? AND profile_id=? AND active=1""",
                (bot_id, str(profile_id)),
            ).fetchone()
        return None if row is None else self._identity(row)

    @staticmethod
    def _identity(row: sqlite3.Row) -> TelegramIdentity:
        return TelegramIdentity(
            telegram_user_id=int(row["telegram_user_id"]),
            profile_id=UUID(str(row["profile_id"])),
            private_chat_id=int(row["private_chat_id"]),
            active=bool(row["active"]),
        )

    def next_offset(self, bot_id: int) -> int | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT next_offset FROM runtimes WHERE bot_id=?", (bot_id,)
            ).fetchone()
        if row is None or row["next_offset"] is None:
            return None
        return int(row["next_offset"])

    def claim_update(
        self,
        *,
        bot_id: int,
        update_id: int,
        owner_id: str,
        lease_seconds: float,
        telegram_user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> ClaimResult:
        if lease_seconds <= 0 or not owner_id:
            raise ValueError("claim owner and lease must be valid")
        now = self._now()
        lease_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM updates WHERE bot_id=? AND update_id=?",
                (bot_id, update_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO updates(
                        bot_id, update_id, telegram_user_id, chat_id, message_id,
                        profile_id, kind, status, received_at, attempt_count,
                        claim_owner, claim_generation, lease_until
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'processing', ?, 1, ?, 1, ?)""",
                    (
                        bot_id,
                        update_id,
                        telegram_user_id,
                        chat_id,
                        message_id,
                        None if profile_id is None else str(profile_id),
                        kind,
                        now.isoformat(),
                        owner_id,
                        lease_until.isoformat(),
                    ),
                )
                connection.execute("COMMIT")
                return ClaimResult(
                    "claimed",
                    UpdateClaim(bot_id, update_id, owner_id, 1, 1, lease_until),
                )

            status = str(row["status"])
            next_retry = self._optional_datetime(row["next_retry_at"])
            existing_lease = self._optional_datetime(row["lease_until"])
            claimable = (
                status == "retryable_error"
                and (next_retry is None or next_retry <= now)
            ) or (
                status == "processing"
                and (existing_lease is None or existing_lease <= now)
            )
            if claimable:
                generation = int(row["claim_generation"]) + 1
                attempts = int(row["attempt_count"]) + 1
                connection.execute(
                    """UPDATE updates SET status='processing', safe_error_code=NULL,
                    next_retry_at=NULL, completed_at=NULL, claim_owner=?,
                    claim_generation=?, attempt_count=?, lease_until=?
                    WHERE bot_id=? AND update_id=?""",
                    (
                        owner_id,
                        generation,
                        attempts,
                        lease_until.isoformat(),
                        bot_id,
                        update_id,
                    ),
                )
                connection.execute("COMMIT")
                return ClaimResult(
                    "claimed",
                    UpdateClaim(
                        bot_id,
                        update_id,
                        owner_id,
                        generation,
                        attempts,
                        lease_until,
                    ),
                )
            connection.execute("COMMIT")
        blocked_until = existing_lease if status == "processing" else next_retry
        return ClaimResult(status, None, blocked_until)

    def renew_claim(
        self, claim: UpdateClaim, lease_seconds: float
    ) -> UpdateClaim | None:
        if lease_seconds <= 0:
            raise ValueError("claim lease must be positive")
        now = self._now()
        renewed_until = now + timedelta(seconds=lease_seconds)
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE updates SET lease_until=? WHERE bot_id=? AND update_id=?
                AND status='processing' AND claim_owner=? AND claim_generation=?
                AND lease_until>?""",
                (
                    renewed_until.isoformat(),
                    claim.bot_id,
                    claim.update_id,
                    claim.owner_id,
                    claim.generation,
                    now.isoformat(),
                ),
            )
        if cursor.rowcount != 1:
            return None
        return UpdateClaim(
            claim.bot_id,
            claim.update_id,
            claim.owner_id,
            claim.generation,
            claim.attempt_count,
            renewed_until,
        )

    def complete_update(
        self, claim: UpdateClaim, status: str, error_code: str | None = None
    ) -> bool:
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE updates SET status=?, safe_error_code=?, completed_at=?,
                next_retry_at=NULL, lease_until=NULL WHERE bot_id=? AND update_id=?
                AND status='processing' AND claim_owner=? AND claim_generation=?
                AND lease_until>?""",
                (
                    status,
                    error_code,
                    now.isoformat(),
                    claim.bot_id,
                    claim.update_id,
                    claim.owner_id,
                    claim.generation,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def defer_update(
        self, claim: UpdateClaim, error_code: str, next_retry_at: datetime
    ) -> bool:
        retry_at = _as_utc(next_retry_at)
        now = self._now()
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE updates SET status='retryable_error', safe_error_code=?,
                next_retry_at=?, completed_at=NULL, lease_until=NULL
                WHERE bot_id=? AND update_id=? AND status='processing'
                AND claim_owner=? AND claim_generation=? AND lease_until>?""",
                (
                    error_code,
                    retry_at.isoformat(),
                    claim.bot_id,
                    claim.update_id,
                    claim.owner_id,
                    claim.generation,
                    now.isoformat(),
                ),
            )
            return cursor.rowcount == 1

    def advance_offset(self, bot_id: int, offset: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runtimes SET next_offset = CASE
                    WHEN next_offset IS NULL OR next_offset < ? THEN ? ELSE next_offset END
                    WHERE bot_id=?""",
                (offset, offset, bot_id),
            )

    def record_poll(self, bot_id: int, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runtimes SET last_poll_at=?, last_error_code=?
                WHERE bot_id=?""",
                (self._now().isoformat(), error_code, bot_id),
            )

    def runtime_status(
        self, bot_id: int
    ) -> tuple[int | None, datetime | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT next_offset, last_poll_at, last_error_code
                FROM runtimes WHERE bot_id=?""",
                (bot_id,),
            ).fetchone()
        if row is None:
            return None, None, None
        return (
            None if row["next_offset"] is None else int(row["next_offset"]),
            self._optional_datetime(row["last_poll_at"]),
            None if row["last_error_code"] is None else str(row["last_error_code"]),
        )

    def delivery_unknown_count(
        self, bot_id: int, profile_id: UUID | None = None
    ) -> int:
        query = (
            "SELECT COUNT(*) FROM outbound_audit WHERE bot_id=? "
            "AND status IN ('unknown', 'reserved')"
        )
        parameters: tuple[object, ...] = (bot_id,)
        if profile_id is not None:
            query += " AND profile_id=?"
            parameters = (bot_id, str(profile_id))
        with self._connect() as connection:
            return int(connection.execute(query, parameters).fetchone()[0])

    def record_attachment(self, claim: UpdateClaim, receipt: InboxReceipt) -> bool:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """SELECT 1 FROM updates WHERE bot_id=? AND update_id=?
                AND status='processing' AND claim_owner=? AND claim_generation=?
                AND lease_until>?""",
                (
                    claim.bot_id,
                    claim.update_id,
                    claim.owner_id,
                    claim.generation,
                    now.isoformat(),
                ),
            ).fetchone()
            if active is None:
                connection.execute("COMMIT")
                return False
            existing = connection.execute(
                """SELECT sha256, size_bytes, status, external_reference
                FROM attachment_audit WHERE bot_id=? AND update_id=?""",
                (claim.bot_id, claim.update_id),
            ).fetchone()
            expected = (
                receipt.sha256,
                receipt.size_bytes,
                receipt.status,
                receipt.external_reference,
            )
            if existing is not None:
                actual = (
                    str(existing["sha256"]),
                    int(existing["size_bytes"]),
                    str(existing["status"]),
                    existing["external_reference"],
                )
                connection.execute("COMMIT")
                return actual == expected
            connection.execute(
                """INSERT INTO attachment_audit(
                    bot_id, update_id, sha256, size_bytes, status, external_reference
                ) VALUES (?, ?, ?, ?, ?, ?)""",
                (claim.bot_id, claim.update_id, *expected),
            )
            connection.execute("COMMIT")
            return True

    def reserve_outbound(
        self,
        *,
        bot_id: int,
        delivery_key: str,
        part_index: int,
        profile_id: UUID,
        chat_id: int,
        content_sha256: str,
    ) -> OutboundReservation:
        now = self._now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT * FROM outbound_audit WHERE bot_id=? AND profile_id=?
                AND delivery_key=? AND part_index=?""",
                (bot_id, str(profile_id), delivery_key, part_index),
            ).fetchone()
            if row is None:
                connection.execute(
                    """INSERT INTO outbound_audit(
                        bot_id, profile_id, delivery_key, part_index, chat_id,
                        content_sha256, status, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)""",
                    (
                        bot_id,
                        str(profile_id),
                        delivery_key,
                        part_index,
                        chat_id,
                        content_sha256,
                        now.isoformat(),
                    ),
                )
                connection.execute("COMMIT")
                return OutboundReservation("claimed")
            if (
                int(row["chat_id"]) != chat_id
                or row["content_sha256"] is None
                or str(row["content_sha256"]) != content_sha256
            ):
                connection.execute("COMMIT")
                return OutboundReservation("conflict")
            status = str(row["status"])
            retry_at = self._optional_datetime(row["next_retry_at"])
            if status == "deferred" and retry_at is not None and retry_at > now:
                connection.execute("COMMIT")
                return OutboundReservation("deferred", retry_at)
            if status in {"deferred", "failed"}:
                connection.execute(
                    """UPDATE outbound_audit SET status='reserved',
                    safe_error_code=NULL, next_retry_at=NULL, completed_at=NULL
                    WHERE bot_id=? AND profile_id=? AND delivery_key=? AND part_index=?""",
                    (bot_id, str(profile_id), delivery_key, part_index),
                )
                connection.execute("COMMIT")
                return OutboundReservation("claimed")
            connection.execute("COMMIT")
        if status == "sent":
            return OutboundReservation("duplicate")
        return OutboundReservation("unknown")

    def mark_outbound_sent(
        self,
        bot_id: int,
        profile_id: UUID,
        delivery_key: str,
        part_index: int,
        telegram_message_id: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE outbound_audit SET status='sent', telegram_message_id=?,
                safe_error_code=NULL, next_retry_at=NULL, completed_at=?
                WHERE bot_id=? AND profile_id=? AND delivery_key=? AND part_index=?
                AND status='reserved'""",
                (
                    telegram_message_id,
                    self._now().isoformat(),
                    bot_id,
                    str(profile_id),
                    delivery_key,
                    part_index,
                ),
            )
            return cursor.rowcount == 1

    def mark_outbound_failed(
        self,
        bot_id: int,
        profile_id: UUID,
        delivery_key: str,
        part_index: int,
        error_code: str,
        *,
        status: str,
        next_retry_at: datetime | None = None,
    ) -> bool:
        if status not in {"failed", "unknown", "deferred"}:
            raise ValueError("invalid outbound status")
        with self._connect() as connection:
            cursor = connection.execute(
                """UPDATE outbound_audit SET status=?, safe_error_code=?,
                next_retry_at=?, completed_at=? WHERE bot_id=? AND profile_id=?
                AND delivery_key=? AND part_index=? AND status='reserved'""",
                (
                    status,
                    error_code,
                    None
                    if next_retry_at is None
                    else _as_utc(next_retry_at).isoformat(),
                    self._now().isoformat(),
                    bot_id,
                    str(profile_id),
                    delivery_key,
                    part_index,
                ),
            )
            return cursor.rowcount == 1

    def audit_columns(self, table: str) -> tuple[str, ...]:
        if table not in {"updates", "attachment_audit", "outbound_audit"}:
            raise ValueError("unsupported audit table")
        with self._connect() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def audit_rows(self, table: str) -> tuple[dict[str, object], ...]:
        if table not in {"updates", "attachment_audit", "outbound_audit"}:
            raise ValueError("unsupported audit table")
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _optional_datetime(value: object) -> datetime | None:
        return None if value is None else datetime.fromisoformat(str(value))
