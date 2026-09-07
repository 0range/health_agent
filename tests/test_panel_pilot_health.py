from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from health_agent.panel.pilot_health import TelegramBotHealthReader
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import TelegramIdentity

PROFILE = UUID("10000000-0000-0000-0000-000000000001")
OTHER = UUID("20000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def _state(path: Path, bot_id: int, profile_id: UUID, polled_at: datetime) -> None:
    state = SqliteTelegramState(path, clock=lambda: polled_at)
    state.register_bot(bot_id, "private")
    state.bind_identity(
        bot_id,
        TelegramIdentity(
            telegram_user_id=7,
            profile_id=profile_id,
            private_chat_id=7,
        ),
    )
    state.record_poll(bot_id)


def test_three_bot_states_are_distinct_profile_bound_and_future_is_not_healthy(
    tmp_path: Path,
) -> None:
    main = tmp_path / "main.sqlite3"
    food = tmp_path / "food.sqlite3"
    training = tmp_path / "training.sqlite3"
    _state(main, 1, PROFILE, NOW - timedelta(seconds=30))
    _state(food, 2, PROFILE, NOW - timedelta(minutes=4))
    _state(training, 3, OTHER, NOW + timedelta(seconds=1))

    reader = TelegramBotHealthReader(
        {"main": main, "food": food, "training": training},
        {"main": tmp_path / "missing-token", "food": None, "training": None},
        clock=lambda: NOW,
    )

    cards = reader.statuses(PROFILE)
    assert [(card.domain, card.status) for card in cards] == [
        ("main", "fresh"),
        ("food", "stale"),
        ("training", "not_bound"),
    ]
    assert cards[0].configured is False  # missing token metadata is safe and non-fatal
    assert cards[0].bound is True
    assert reader.statuses(OTHER)[2].status == "future"


def test_missing_state_is_unavailable_and_get_does_not_create_database(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nested" / "state.sqlite3"
    reader = TelegramBotHealthReader(
        {"main": missing, "food": missing, "training": missing}, clock=lambda: NOW
    )

    assert all(card.status == "unavailable" for card in reader.statuses(PROFILE))
    assert not missing.exists()
    assert not missing.parent.exists()
