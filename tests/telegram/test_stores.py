from __future__ import annotations

import os
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from health_agent.telegram.stores import (
    LEGACY_BOT_ID,
    PrivateBotTokenStore,
    SqliteTelegramState,
    TelegramIdentityConflict,
)
from health_agent.telegram.types import (
    InboxReceipt,
    TelegramIdentity,
    VerifiedBotCredential,
)


@dataclass
class Clock:
    value: datetime = datetime(2026, 9, 4, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


def _state(tmp_path, clock: Clock | None = None) -> SqliteTelegramState:
    result = SqliteTelegramState(
        tmp_path / "private" / "state.sqlite3", clock=clock or Clock()
    )
    result.register_bot(111, "first_bot")
    result.register_bot(222, "second_bot")
    return result


def test_verified_credential_and_state_are_private_and_content_free(tmp_path) -> None:
    token_path = tmp_path / "private" / "bot-token"
    tokens = PrivateBotTokenStore(token_path)
    tokens.save_verified(VerifiedBotCredential("123456:very-secret-value", 111, "bot"))
    state = _state(tmp_path)

    assert tokens.load_verified() == VerifiedBotCredential(
        "123456:very-secret-value", 111, "bot"
    )
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    for table in ("updates", "attachment_audit", "outbound_audit"):
        columns = state.audit_columns(table)
        assert "text" not in columns
        assert "content" not in columns
        assert "token" not in columns


def test_private_paths_reject_symlink_parent_and_targets(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    parent_link = tmp_path / "linked"
    parent_link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        PrivateBotTokenStore(parent_link / "token").save_verified(
            VerifiedBotCredential("123:secret", 111)
        )

    token_target = real / "token-target"
    token_target.write_text("do not replace")
    token_link = tmp_path / "token"
    token_link.symlink_to(token_target)
    with pytest.raises(ValueError, match="symlink"):
        PrivateBotTokenStore(token_link).save_verified(
            VerifiedBotCredential("123:secret", 111)
        )

    sqlite_target = real / "state.sqlite3"
    sqlite3.connect(sqlite_target).close()
    sqlite_link = tmp_path / "state.sqlite3"
    sqlite_link.symlink_to(sqlite_target)
    with pytest.raises(ValueError, match="symlink"):
        SqliteTelegramState(sqlite_link)


def test_identity_binding_is_transactional_and_bot_scoped(tmp_path) -> None:
    state = _state(tmp_path)
    first_profile = uuid4()
    second_profile = uuid4()
    committed = state.bind_identity(111, TelegramIdentity(101, first_profile, 101))
    state.bind_identity(222, TelegramIdentity(101, second_profile, 101))

    assert committed.profile_id == first_profile
    assert state.identity_for_user(111, 101).profile_id == first_profile  # type: ignore[union-attr]
    assert state.identity_for_user(222, 101).profile_id == second_profile  # type: ignore[union-attr]
    with pytest.raises(TelegramIdentityConflict):
        state.bind_identity(111, TelegramIdentity(101, second_profile, 101))
    with pytest.raises(TelegramIdentityConflict):
        state.bind_identity(111, TelegramIdentity(202, first_profile, 202))


def test_concurrent_bind_returns_only_the_identity_actually_committed(tmp_path) -> None:
    state = _state(tmp_path)
    profiles = (uuid4(), uuid4())
    barrier = threading.Barrier(2)
    committed: list[TelegramIdentity] = []
    conflicts: list[TelegramIdentityConflict] = []

    def bind(profile_id) -> None:
        barrier.wait()
        try:
            committed.append(
                state.bind_identity(111, TelegramIdentity(101, profile_id, 101))
            )
        except TelegramIdentityConflict as error:
            conflicts.append(error)

    workers = [threading.Thread(target=bind, args=(profile,)) for profile in profiles]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=2)

    assert len(committed) == 1
    assert len(conflicts) == 1
    assert state.identity_for_user(111, 101) == committed[0]


def test_claim_renewal_prevents_reclaim_and_stale_completion_is_fenced(
    tmp_path,
) -> None:
    clock = Clock()
    state = _state(tmp_path, clock)
    profile_id = uuid4()
    first = state.claim_update(
        bot_id=111,
        update_id=7,
        owner_id="worker-a",
        lease_seconds=60,
        telegram_user_id=101,
        chat_id=101,
        message_id=8,
        profile_id=profile_id,
        kind="text",
    ).claim
    assert first is not None
    clock.advance(40)
    renewed = state.renew_claim(first, 60)
    assert renewed is not None
    clock.advance(30)  # past the original lease, inside the renewed lease
    blocked = state.claim_update(
        bot_id=111,
        update_id=7,
        owner_id="worker-b",
        lease_seconds=60,
        telegram_user_id=101,
        chat_id=101,
        message_id=8,
        profile_id=profile_id,
        kind="text",
    )
    assert blocked.claim is None
    assert blocked.status == "processing"
    assert blocked.next_retry_at == renewed.lease_until

    clock.advance(31)
    second = state.claim_update(
        bot_id=111,
        update_id=7,
        owner_id="worker-b",
        lease_seconds=60,
        telegram_user_id=101,
        chat_id=101,
        message_id=8,
        profile_id=profile_id,
        kind="text",
    ).claim
    assert second is not None
    assert second.generation == first.generation + 1
    assert not state.complete_update(first, "replied")
    assert state.complete_update(second, "replied")


def test_deferred_claim_is_not_retried_before_due_time(tmp_path) -> None:
    clock = Clock()
    state = _state(tmp_path, clock)
    claim = state.claim_update(
        bot_id=111,
        update_id=8,
        owner_id="worker-a",
        lease_seconds=60,
        telegram_user_id=None,
        chat_id=None,
        message_id=None,
        profile_id=None,
        kind="text",
    ).claim
    assert claim is not None
    due = clock.value + timedelta(minutes=3)
    assert state.defer_update(claim, "temporary", due)

    blocked = state.claim_update(
        bot_id=111,
        update_id=8,
        owner_id="worker-b",
        lease_seconds=60,
        telegram_user_id=None,
        chat_id=None,
        message_id=None,
        profile_id=None,
        kind="text",
    )
    assert blocked.status == "retryable_error"
    assert blocked.claim is None
    assert blocked.next_retry_at == due
    clock.advance(180)
    retried = state.claim_update(
        bot_id=111,
        update_id=8,
        owner_id="worker-b",
        lease_seconds=60,
        telegram_user_id=None,
        chat_id=None,
        message_id=None,
        profile_id=None,
        kind="text",
    ).claim
    assert retried is not None
    assert retried.attempt_count == 2


def test_expired_claim_cannot_complete_even_before_another_worker_reclaims(
    tmp_path,
) -> None:
    clock = Clock()
    state = _state(tmp_path, clock)
    claim = state.claim_update(
        bot_id=111,
        update_id=9,
        owner_id="worker-a",
        lease_seconds=10,
        telegram_user_id=None,
        chat_id=None,
        message_id=None,
        profile_id=None,
        kind="text",
    ).claim
    assert claim is not None
    clock.advance(11)

    assert not state.complete_update(claim, "replied")


def test_attachment_audit_is_idempotent_for_a_valid_reclaimed_update(tmp_path) -> None:
    clock = Clock()
    state = _state(tmp_path, clock)
    first = state.claim_update(
        bot_id=111,
        update_id=10,
        owner_id="worker-a",
        lease_seconds=10,
        telegram_user_id=101,
        chat_id=101,
        message_id=20,
        profile_id=uuid4(),
        kind="document",
    ).claim
    assert first is not None
    receipt = InboxReceipt("a" * 64, 42, "accepted", "not persisted", "doc:1")
    assert state.record_attachment(first, receipt)
    clock.advance(11)
    second = state.claim_update(
        bot_id=111,
        update_id=10,
        owner_id="worker-b",
        lease_seconds=10,
        telegram_user_id=101,
        chat_id=101,
        message_id=20,
        profile_id=uuid4(),
        kind="document",
    ).claim
    assert second is not None

    assert state.record_attachment(second, receipt)
    assert len(state.audit_rows("attachment_audit")) == 1
    mismatched = InboxReceipt("b" * 64, 42, "accepted", "not persisted", "doc:1")
    assert not state.record_attachment(second, mismatched)


def test_outbound_keys_are_bot_and_profile_scoped_with_content_guard(tmp_path) -> None:
    state = _state(tmp_path)
    first = uuid4()
    second = uuid4()

    for bot_id, profile_id in ((111, first), (111, second), (222, first)):
        reservation = state.reserve_outbound(
            bot_id=bot_id,
            delivery_key="checkup:2027",
            part_index=0,
            profile_id=profile_id,
            chat_id=101,
            content_sha256="a" * 64,
        )
        assert reservation.status == "claimed"
        assert state.mark_outbound_sent(
            bot_id, profile_id, "checkup:2027", 0, telegram_message_id=1
        )

    duplicate = state.reserve_outbound(
        bot_id=111,
        delivery_key="checkup:2027",
        part_index=0,
        profile_id=first,
        chat_id=101,
        content_sha256="a" * 64,
    )
    conflict = state.reserve_outbound(
        bot_id=111,
        delivery_key="checkup:2027",
        part_index=0,
        profile_id=first,
        chat_id=101,
        content_sha256="b" * 64,
    )
    assert duplicate.status == "duplicate"
    assert conflict.status == "conflict"


def test_legacy_sqlite_is_migrated_losslessly_into_bot_zero_namespace(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    profile_id = uuid4()
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE identities (
              telegram_user_id INTEGER PRIMARY KEY, profile_id TEXT UNIQUE NOT NULL,
              private_chat_id INTEGER NOT NULL, active INTEGER NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE runtime (
              singleton INTEGER PRIMARY KEY, next_offset INTEGER,
              last_poll_at TEXT, last_error_code TEXT
            );
            CREATE TABLE updates (
              update_id INTEGER PRIMARY KEY, telegram_user_id INTEGER, chat_id INTEGER,
              message_id INTEGER, profile_id TEXT, kind TEXT NOT NULL, status TEXT NOT NULL,
              safe_error_code TEXT, received_at TEXT NOT NULL, completed_at TEXT
            );
            CREATE TABLE attachment_audit (
              update_id INTEGER PRIMARY KEY, sha256 TEXT NOT NULL, size_bytes INTEGER NOT NULL,
              status TEXT NOT NULL, external_reference TEXT
            );
            CREATE TABLE outbound_audit (
              delivery_key TEXT NOT NULL, part_index INTEGER NOT NULL, profile_id TEXT NOT NULL,
              chat_id INTEGER NOT NULL, status TEXT NOT NULL, telegram_message_id INTEGER,
              safe_error_code TEXT, created_at TEXT NOT NULL, completed_at TEXT,
              PRIMARY KEY(delivery_key, part_index)
            );
            """
        )
        connection.execute(
            "INSERT INTO identities VALUES (101, ?, 101, 1, ?)",
            (str(profile_id), Clock().value.isoformat()),
        )
        connection.execute("INSERT INTO runtime VALUES (1, 42, NULL, NULL)")
        connection.execute(
            "INSERT INTO outbound_audit VALUES (?, 0, ?, 101, 'sent', 9, NULL, ?, ?)",
            (
                "legacy-reminder",
                str(profile_id),
                Clock().value.isoformat(),
                Clock().value.isoformat(),
            ),
        )

    migrated = SqliteTelegramState(path, clock=Clock())
    reopened = SqliteTelegramState(path, clock=Clock())

    assert migrated.identity_for_user(LEGACY_BOT_ID, 101).profile_id == profile_id  # type: ignore[union-attr]
    assert reopened.next_offset(LEGACY_BOT_ID) == 42
    outbound = reopened.audit_rows("outbound_audit")[0]
    assert outbound["bot_id"] == LEGACY_BOT_ID
    assert outbound["profile_id"] == str(profile_id)
    assert outbound["delivery_key"] == "legacy-reminder"
    assert os.stat(path).st_mode & 0o777 == 0o600
