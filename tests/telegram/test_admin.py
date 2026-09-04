from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from health_agent.telegram.admin import TelegramAdminService, TelegramIdentityConflict
from health_agent.telegram.api import TelegramAPIError
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState


@dataclass
class FakeProfiles:
    ids: set[UUID]

    def exists(self, profile_id: UUID) -> bool:
        return profile_id in self.ids


class FakeGateway:
    def __init__(self, bot_id: int, username: str, webhook: str = "") -> None:
        self.bot_id = bot_id
        self.username = username
        self.webhook = webhook

    def get_me(self) -> dict[str, object]:
        return {"id": self.bot_id, "is_bot": True, "username": self.username}

    def get_webhook_url(self) -> str:
        return self.webhook


def _service(tmp_path, profiles: set[UUID], gateways: dict[str, FakeGateway]):
    return TelegramAdminService(
        PrivateBotTokenStore(tmp_path / "bot-token"),
        SqliteTelegramState(tmp_path / "state.sqlite3"),
        FakeProfiles(profiles),
        gateway_factory=gateways.__getitem__,  # type: ignore[arg-type]
        clock=lambda: datetime(2026, 9, 4, tzinfo=UTC),
    )


def test_configuration_verifies_get_me_and_separates_replacement_bot(tmp_path) -> None:
    token_a = "111:first-secret"
    token_b = "222:second-secret"
    profile_id = uuid4()
    service = _service(
        tmp_path,
        {profile_id},
        {
            token_a: FakeGateway(111, "first_bot"),
            token_b: FakeGateway(222, "second_bot"),
        },
    )

    assert service.configure_token(token_a).bot_id == 111
    service.bind_identity(profile_id, 101)
    service.state.advance_offset(111, 500)
    old_claim = service.state.claim_update(
        bot_id=111,
        update_id=7,
        owner_id="old-worker",
        lease_seconds=60,
        telegram_user_id=101,
        chat_id=101,
        message_id=1,
        profile_id=profile_id,
        kind="text",
    ).claim
    assert old_claim is not None
    assert service.state.complete_update(old_claim, "replied")
    assert service.configure_token(token_b).bot_id == 222

    assert service.tokens.load_verified().bot_id == 222
    assert service.state.next_offset(222) is None
    assert service.state.next_offset(111) == 500
    assert (
        service.state.claim_update(
            bot_id=222,
            update_id=7,
            owner_id="new-worker",
            lease_seconds=60,
            telegram_user_id=101,
            chat_id=101,
            message_id=1,
            profile_id=profile_id,
            kind="text",
        ).claim
        is not None
    )
    assert not service.status(profile_id).identity_bound


def test_invalid_get_me_does_not_publish_candidate_token(tmp_path) -> None:
    token = "111:invalid-secret"

    class InvalidGateway(FakeGateway):
        def get_me(self) -> dict[str, object]:
            return {"id": "not-an-integer", "is_bot": True}

    service = _service(tmp_path, set(), {token: InvalidGateway(111, "invalid")})

    with pytest.raises(TelegramAPIError, match="invalid_get_me_response"):
        service.configure_token(token)
    assert not service.tokens.exists()


def test_admin_validates_profile_and_prevents_cross_profile_binding(tmp_path) -> None:
    first = uuid4()
    second = uuid4()
    token = "111:secret"
    service = _service(tmp_path, {first, second}, {token: FakeGateway(111, "bot")})
    service.configure_token(token)
    service.bind_identity(first, 101)

    with pytest.raises(TelegramIdentityConflict):
        service.bind_identity(second, 101)
    with pytest.raises(TelegramIdentityConflict):
        service.bind_identity(first, 202)
    with pytest.raises(ValueError, match="profile does not exist"):
        service.bind_identity(uuid4(), 303)


def test_status_verifies_remote_bot_webhook_and_heartbeat(tmp_path) -> None:
    profile = uuid4()
    token = "111:secret"
    gateway = FakeGateway(111, "health_bot")
    service = _service(tmp_path, {profile}, {token: gateway})
    service.configure_token(token)
    service.bind_identity(profile, 101)
    service.state.record_poll(111)
    reservation = service.state.reserve_outbound(
        bot_id=111,
        profile_id=profile,
        delivery_key="reply:unknown",
        part_index=0,
        chat_id=101,
        content_sha256="a" * 64,
    )
    assert reservation.status == "claimed"
    assert service.state.mark_outbound_failed(
        111,
        profile,
        "reply:unknown",
        0,
        "delivery_transport_unknown",
        status="unknown",
    )

    status = service.status(profile)

    assert status.token_configured
    assert status.credential_verified
    assert status.bot_id == 111
    assert status.bot_username == "health_bot"
    assert status.webhook_configured is False
    assert status.poller_running
    assert status.delivery_unknown_count == 1
    assert status.identity_bound

    gateway.webhook = "https://example.invalid/hook"
    blocked = service.status(profile)
    assert blocked.webhook_configured is True
    assert not blocked.poller_running
    assert blocked.last_error_code == "webhook_configured"

    def unavailable_webhook() -> str:
        raise TelegramAPIError("api_transport_error")

    gateway.get_webhook_url = unavailable_webhook  # type: ignore[method-assign]
    unavailable = service.status(profile)
    assert unavailable.credential_verified
    assert unavailable.webhook_configured is None
    assert not unavailable.poller_running
    assert unavailable.last_error_code == "api_transport_error"
