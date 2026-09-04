from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from health_agent.telegram.api import TelegramDeferred, TelegramDeliveryUnknown
from health_agent.telegram.messenger import TelegramMessenger, split_message
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import TelegramIdentity


@dataclass
class FakeGateway:
    sent: list[tuple[int, str]] = field(default_factory=list)
    failure: Exception | None = None

    def send_message(self, chat_id: int, text: str) -> int:
        if self.failure is not None:
            raise self.failure
        self.sent.append((chat_id, text))
        return len(self.sent)


def _messenger(tmp_path, gateway: FakeGateway, bot_id: int = 111):
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    state.register_bot(bot_id, "bot")
    return state, TelegramMessenger(bot_id, gateway, state)  # type: ignore[arg-type]


def test_message_split_stays_within_official_limit() -> None:
    parts = split_message(("word " * 2000).strip())

    assert len(parts) > 1
    assert all(0 < len(part) <= 4096 for part in parts)
    assert " ".join(parts).split() == ("word " * 2000).split()


def test_repeated_delivery_key_does_not_send_twice(tmp_path) -> None:
    gateway = FakeGateway()
    state, messenger = _messenger(tmp_path, gateway)
    profile_id = uuid4()
    state.bind_identity(111, TelegramIdentity(101, profile_id, 101))

    first = messenger.send_to_profile(profile_id, "reminder", delivery_key="checkup:1")
    second = messenger.send_to_profile(profile_id, "reminder", delivery_key="checkup:1")

    assert first.sent == 1
    assert second.sent == 0
    assert second.previously_sent == 1
    assert gateway.sent == [(101, "reminder")]


def test_same_business_key_for_two_profiles_sends_both(tmp_path) -> None:
    gateway = FakeGateway()
    state, messenger = _messenger(tmp_path, gateway)
    first = uuid4()
    second = uuid4()
    state.bind_identity(111, TelegramIdentity(101, first, 101))
    state.bind_identity(111, TelegramIdentity(202, second, 202))

    messenger.send_to_profile(first, "checkup", delivery_key="checkup:2027")
    messenger.send_to_profile(second, "checkup", delivery_key="checkup:2027")

    assert gateway.sent == [(101, "checkup"), (202, "checkup")]


def test_unknown_delivery_is_persisted_and_never_automatically_retried(
    tmp_path,
) -> None:
    gateway = FakeGateway(failure=TelegramDeliveryUnknown())
    state, messenger = _messenger(tmp_path, gateway)
    profile_id = uuid4()

    with pytest.raises(TelegramDeliveryUnknown):
        messenger.send_to_chat(profile_id, 101, "reply", delivery_key="reply:1")
    gateway.failure = None
    with pytest.raises(TelegramDeliveryUnknown):
        messenger.send_to_chat(profile_id, 101, "reply", delivery_key="reply:1")

    assert not gateway.sent
    assert state.audit_rows("outbound_audit")[0]["status"] == "unknown"


def test_rate_limited_delivery_stays_deferred_until_full_retry_time(tmp_path) -> None:
    retry_at = datetime.now(UTC) + timedelta(hours=1)
    gateway = FakeGateway(failure=TelegramDeferred(retry_at))
    _, messenger = _messenger(tmp_path, gateway)
    profile_id = uuid4()

    with pytest.raises(TelegramDeferred) as first:
        messenger.send_to_chat(profile_id, 101, "reply", delivery_key="reply:2")
    gateway.failure = None
    with pytest.raises(TelegramDeferred) as second:
        messenger.send_to_chat(profile_id, 101, "reply", delivery_key="reply:2")

    assert first.value.retry_at == retry_at
    assert second.value.retry_at == retry_at
    assert not gateway.sent
