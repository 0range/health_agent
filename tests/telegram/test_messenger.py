from __future__ import annotations

from uuid import uuid4

from health_agent.telegram.messenger import TelegramMessenger, split_message
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import TelegramIdentity


class FakeGateway:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []

    def send_message(self, chat_id: int, text: str) -> int:
        self.sent.append((chat_id, text))
        return len(self.sent)


def test_message_split_stays_within_official_limit() -> None:
    parts = split_message(("word " * 2000).strip())

    assert len(parts) > 1
    assert all(0 < len(part) <= 4096 for part in parts)
    assert " ".join(parts).split() == ("word " * 2000).split()


def test_repeated_delivery_key_does_not_send_twice(tmp_path) -> None:
    state = SqliteTelegramState(tmp_path / "state.sqlite3")
    profile_id = uuid4()
    state.bind_identity(TelegramIdentity(101, profile_id, 101))
    gateway = FakeGateway()
    messenger = TelegramMessenger(gateway, state)  # type: ignore[arg-type]

    first = messenger.send_to_profile(profile_id, "reminder", delivery_key="checkup:1")
    second = messenger.send_to_profile(profile_id, "reminder", delivery_key="checkup:1")

    assert first.sent == 1
    assert second.sent == 0
    assert second.previously_reserved == 1
    assert gateway.sent == [(101, "reminder")]
