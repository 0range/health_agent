"""Private token storage and durable, content-free Telegram operational state."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from health_agent.telegram.types import InboxReceipt, TelegramIdentity


def _utc_text() -> str:
    return datetime.now(UTC).isoformat()


def _private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


def _atomic_private_write(path: Path, value: str) -> None:
    _private_directory(path.parent)
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
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


class PrivateBotTokenStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def save(self, token: str) -> None:
        token = token.strip()
        if (
            not token
            or ":" not in token
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Telegram bot token has an invalid format")
        _atomic_private_write(self.path, token)

    def load(self) -> str:
        if self.path.is_symlink() or not self.path.is_file():
            raise FileNotFoundError("Telegram bot token is not configured")
        self.path.chmod(0o600)
        token = self.path.read_text(encoding="utf-8").strip()
        if (
            not token
            or ":" not in token
            or any(character.isspace() for character in token)
        ):
            raise ValueError("Stored Telegram bot token has an invalid format")
        return token

    def exists(self) -> bool:
        return self.path.is_file() and not self.path.is_symlink()


class SqliteTelegramState:
    """Small replaceable store; it deliberately contains no dialogue or file bytes."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        _private_directory(self.path.parent)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        self.path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS identities (
                    telegram_user_id INTEGER PRIMARY KEY,
                    profile_id TEXT NOT NULL UNIQUE,
                    private_chat_id INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    next_offset INTEGER,
                    last_poll_at TEXT,
                    last_error_code TEXT
                );
                INSERT OR IGNORE INTO runtime(singleton) VALUES (1);
                CREATE TABLE IF NOT EXISTS updates (
                    update_id INTEGER PRIMARY KEY,
                    telegram_user_id INTEGER,
                    chat_id INTEGER,
                    message_id INTEGER,
                    profile_id TEXT,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    safe_error_code TEXT,
                    received_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS attachment_audit (
                    update_id INTEGER PRIMARY KEY REFERENCES updates(update_id),
                    sha256 TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0),
                    status TEXT NOT NULL,
                    external_reference TEXT
                );
                CREATE TABLE IF NOT EXISTS outbound_audit (
                    delivery_key TEXT NOT NULL,
                    part_index INTEGER NOT NULL CHECK (part_index >= 0),
                    profile_id TEXT NOT NULL,
                    chat_id INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    telegram_message_id INTEGER,
                    safe_error_code TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    PRIMARY KEY (delivery_key, part_index)
                );
                """
            )
        self.path.chmod(0o600)

    def bind_identity(self, identity: TelegramIdentity) -> None:
        with self._connect() as connection:
            # Inactive rows are historical configuration, not live ownership. Remove
            # only the two inactive endpoints involved so either side can be rebound.
            connection.execute(
                """DELETE FROM identities
                WHERE active=0 AND (telegram_user_id=? OR profile_id=?)""",
                (identity.telegram_user_id, str(identity.profile_id)),
            )
            connection.execute(
                """
                INSERT INTO identities(
                    telegram_user_id, profile_id, private_chat_id, active, created_at
                ) VALUES (?, ?, ?, 1, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    private_chat_id=excluded.private_chat_id,
                    active=1
                """,
                (
                    identity.telegram_user_id,
                    str(identity.profile_id),
                    identity.private_chat_id,
                    _utc_text(),
                ),
            )

    def unbind_identity(self, profile_id: UUID) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE identities SET active=0 WHERE profile_id=? AND active=1",
                (str(profile_id),),
            )
            return cursor.rowcount == 1

    def identity_for_user(self, telegram_user_id: int) -> TelegramIdentity | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT telegram_user_id, profile_id, private_chat_id, active
                FROM identities WHERE telegram_user_id=? AND active=1""",
                (telegram_user_id,),
            ).fetchone()
        if row is None:
            return None
        return TelegramIdentity(
            telegram_user_id=int(row["telegram_user_id"]),
            profile_id=UUID(str(row["profile_id"])),
            private_chat_id=int(row["private_chat_id"]),
            active=bool(row["active"]),
        )

    def identity_for_profile(self, profile_id: UUID) -> TelegramIdentity | None:
        with self._connect() as connection:
            row = connection.execute(
                """SELECT telegram_user_id, profile_id, private_chat_id, active
                FROM identities WHERE profile_id=? AND active=1""",
                (str(profile_id),),
            ).fetchone()
        if row is None:
            return None
        return TelegramIdentity(
            telegram_user_id=int(row["telegram_user_id"]),
            profile_id=UUID(str(row["profile_id"])),
            private_chat_id=int(row["private_chat_id"]),
            active=bool(row["active"]),
        )

    def next_offset(self) -> int | None:
        with self._connect() as connection:
            value = connection.execute(
                "SELECT next_offset FROM runtime WHERE singleton=1"
            ).fetchone()[0]
        return None if value is None else int(value)

    def begin_update(
        self,
        *,
        update_id: int,
        telegram_user_id: int | None,
        chat_id: int | None,
        message_id: int | None,
        profile_id: UUID | None,
        kind: str,
    ) -> str:
        now = datetime.now(UTC)
        with self._connect() as connection:
            inserted = connection.execute(
                """
                INSERT OR IGNORE INTO updates(
                    update_id, telegram_user_id, chat_id, message_id, profile_id,
                    kind, status, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'processing', ?)
                """,
                (
                    update_id,
                    telegram_user_id,
                    chat_id,
                    message_id,
                    None if profile_id is None else str(profile_id),
                    kind,
                    now.isoformat(),
                ),
            )
            if inserted.rowcount == 1:
                return "claimed"
            # A reported transient failure is safe to retry. A process crash may
            # leave a claim behind; reclaim it only after a conservative lease.
            reclaimed = connection.execute(
                """UPDATE updates SET status='processing', safe_error_code=NULL,
                received_at=?, completed_at=NULL
                WHERE update_id=? AND (
                    status='retryable_error' OR
                    (status='processing' AND received_at < ?)
                )""",
                (
                    now.isoformat(),
                    update_id,
                    (now - timedelta(minutes=5)).isoformat(),
                ),
            )
            if reclaimed.rowcount == 1:
                return "claimed"
            row = connection.execute(
                "SELECT status FROM updates WHERE update_id=?", (update_id,)
            ).fetchone()
        return str(row["status"])

    def complete_update(
        self, update_id: int, status: str, error_code: str | None = None
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE updates SET status=?, safe_error_code=?, completed_at=?
                WHERE update_id=?""",
                (status, error_code, _utc_text(), update_id),
            )

    def advance_offset(self, offset: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE runtime SET next_offset = CASE
                    WHEN next_offset IS NULL OR next_offset < ? THEN ? ELSE next_offset END
                WHERE singleton=1""",
                (offset, offset),
            )

    def record_poll(self, error_code: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE runtime SET last_poll_at=?, last_error_code=? WHERE singleton=1",
                (_utc_text(), error_code),
            )

    def runtime_status(self) -> tuple[int | None, datetime | None, str | None]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT next_offset, last_poll_at, last_error_code FROM runtime WHERE singleton=1"
            ).fetchone()
        last_poll = None
        if row["last_poll_at"] is not None:
            last_poll = datetime.fromisoformat(str(row["last_poll_at"]))
        offset = None if row["next_offset"] is None else int(row["next_offset"])
        error = None if row["last_error_code"] is None else str(row["last_error_code"])
        return offset, last_poll, error

    def record_attachment(self, update_id: int, receipt: InboxReceipt) -> None:
        with self._connect() as connection:
            connection.execute(
                """INSERT OR IGNORE INTO attachment_audit(
                    update_id, sha256, size_bytes, status, external_reference
                ) VALUES (?, ?, ?, ?, ?)""",
                (
                    update_id,
                    receipt.sha256,
                    receipt.size_bytes,
                    receipt.status,
                    receipt.external_reference,
                ),
            )

    def reserve_outbound(
        self,
        *,
        delivery_key: str,
        part_index: int,
        profile_id: UUID,
        chat_id: int,
    ) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """INSERT OR IGNORE INTO outbound_audit(
                    delivery_key, part_index, profile_id, chat_id, status, created_at
                ) VALUES (?, ?, ?, ?, 'reserved', ?)""",
                (delivery_key, part_index, str(profile_id), chat_id, _utc_text()),
            )
            return cursor.rowcount == 1

    def mark_outbound_sent(
        self, delivery_key: str, part_index: int, telegram_message_id: int
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE outbound_audit SET status='sent', telegram_message_id=?,
                safe_error_code=NULL, completed_at=?
                WHERE delivery_key=? AND part_index=?""",
                (telegram_message_id, _utc_text(), delivery_key, part_index),
            )

    def mark_outbound_failed(
        self, delivery_key: str, part_index: int, error_code: str
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """UPDATE outbound_audit SET status='failed', safe_error_code=?, completed_at=?
                WHERE delivery_key=? AND part_index=?""",
                (error_code, _utc_text(), delivery_key, part_index),
            )

    def audit_columns(self, table: str) -> tuple[str, ...]:
        if table not in {"updates", "attachment_audit", "outbound_audit"}:
            raise ValueError("unsupported audit table")
        with self._connect() as connection:
            rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
        return tuple(str(row["name"]) for row in rows)

    def audit_rows(self, table: str) -> tuple[dict[str, object], ...]:
        """Return sanitized operational rows for local status/tests only."""
        if table not in {"updates", "attachment_audit", "outbound_audit"}:
            raise ValueError("unsupported audit table")
        with self._connect() as connection:
            rows = connection.execute(f"SELECT * FROM {table}").fetchall()
        return tuple(dict(row) for row in rows)
