from __future__ import annotations

import stat
from uuid import uuid4

from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import InboxReceipt, TelegramIdentity


def test_token_and_state_are_private_and_content_free(tmp_path) -> None:
    token_path = tmp_path / "private" / "bot-token"
    state_path = tmp_path / "private" / "state.sqlite3"
    tokens = PrivateBotTokenStore(token_path)
    tokens.save("123456:very-secret-value")
    state = SqliteTelegramState(state_path)

    assert tokens.load() == "123456:very-secret-value"
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700
    for table in ("updates", "attachment_audit", "outbound_audit"):
        columns = state.audit_columns(table)
        assert "text" not in columns
        assert "content" not in columns
        assert "token" not in columns


def test_identity_is_one_to_one_and_can_be_rebound_after_unbind(tmp_path) -> None:
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    first_profile = uuid4()
    second_profile = uuid4()
    state.bind_identity(TelegramIdentity(101, first_profile, 101))
    state.bind_identity(TelegramIdentity(202, second_profile, 202))

    assert state.identity_for_user(101).profile_id == first_profile  # type: ignore[union-attr]
    assert state.identity_for_profile(second_profile).telegram_user_id == 202  # type: ignore[union-attr]
    assert state.unbind_identity(first_profile)
    state.bind_identity(TelegramIdentity(303, first_profile, 303))

    assert state.identity_for_user(101) is None
    assert state.identity_for_profile(first_profile).telegram_user_id == 303  # type: ignore[union-attr]


def test_update_offset_and_outbound_reservations_are_durable(tmp_path) -> None:
    state_path = tmp_path / "state.sqlite3"
    state = SqliteTelegramState(state_path)
    profile_id = uuid4()

    assert (
        state.begin_update(
            update_id=7,
            telegram_user_id=101,
            chat_id=101,
            message_id=8,
            profile_id=profile_id,
            kind="text",
        )
        == "claimed"
    )
    state.complete_update(7, "replied")
    state.advance_offset(8)
    state.advance_offset(3)
    assert (
        state.begin_update(
            update_id=7,
            telegram_user_id=101,
            chat_id=101,
            message_id=8,
            profile_id=profile_id,
            kind="text",
        )
        == "replied"
    )
    assert SqliteTelegramState(state_path).next_offset() == 8

    assert state.reserve_outbound(
        delivery_key="reminder:1", part_index=0, profile_id=profile_id, chat_id=101
    )
    assert not state.reserve_outbound(
        delivery_key="reminder:1", part_index=0, profile_id=profile_id, chat_id=101
    )


def test_attachment_audit_has_only_hash_size_status_and_reference(tmp_path) -> None:
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    profile_id = uuid4()
    state.begin_update(
        update_id=1,
        telegram_user_id=1,
        chat_id=1,
        message_id=1,
        profile_id=profile_id,
        kind="document",
    )
    state.record_attachment(
        1,
        InboxReceipt(
            sha256="a" * 64,
            size_bytes=12,
            status="accepted",
            reply_text="sensitive reply",
            external_reference="document-id",
        ),
    )

    row = state.audit_rows("attachment_audit")[0]
    assert row == {
        "update_id": 1,
        "sha256": "a" * 64,
        "size_bytes": 12,
        "status": "accepted",
        "external_reference": "document-id",
    }
    assert "sensitive reply" not in repr(state.audit_rows("attachment_audit"))
