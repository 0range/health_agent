from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from health_agent.panel.pilot_health import TelegramBotHealthReader

PROFILE = UUID("10000000-0000-0000-0000-000000000001")
OTHER = UUID("20000000-0000-0000-0000-000000000002")
NOW = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)


def _state(path: Path, bot_id: int, profile_id: UUID, polled_at: datetime) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE bot_namespaces (bot_id INTEGER PRIMARY KEY, username TEXT, verified_at TEXT NOT NULL);
        CREATE TABLE identities (bot_id INTEGER, telegram_user_id INTEGER, profile_id TEXT, private_chat_id INTEGER, active INTEGER);
        CREATE TABLE runtimes (bot_id INTEGER PRIMARY KEY, next_offset INTEGER, last_poll_at TEXT, last_error_code TEXT);
        """
    )
    connection.execute(
        "INSERT INTO bot_namespaces VALUES (?, ?, ?)",
        (bot_id, "private", NOW.isoformat()),
    )
    connection.execute(
        "INSERT INTO identities VALUES (?, 7, ?, 7, 1)", (bot_id, str(profile_id))
    )
    connection.execute(
        "INSERT INTO runtimes VALUES (?, NULL, ?, NULL)",
        (bot_id, polled_at.isoformat()),
    )
    connection.commit()
    connection.close()


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
