"""Bounded reminder series driven only by durable delivery receipts."""

from __future__ import annotations

from datetime import datetime, timedelta

from health_agent.pilot.contracts import Notice, Record

SPACING = timedelta(minutes=30)
LIMIT = 3


def delivered_at(receipt: Record) -> datetime:
    value = receipt.payload.get("delivered_at")
    at = datetime.fromisoformat(value) if isinstance(value, str) else receipt.at
    if at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("notice_timestamp_must_be_aware")
    return at


def deadline(receipts: list[Record], initial_expiry: datetime) -> datetime:
    # Allow scheduler jitter at the third notification and bounded recovery after sleep.
    return (
        max(initial_expiry, min(map(delivered_at, receipts)) + timedelta(minutes=90))
        if receipts
        else initial_expiry
    )


def next_notice(
    receipts: list[Record],
    *,
    base_key: str,
    target: datetime,
    initial_expiry: datetime,
    now: datetime,
    meal_name: str,
) -> Notice | None:
    if (
        len(receipts) >= LIMIT
        or now < target
        or now > deadline(receipts, initial_expiry)
    ):
        return None
    if receipts and now < max(map(delivered_at, receipts)) + SPACING:
        return None
    attempt = len(receipts) + 1
    key = base_key if attempt == 1 else f"{base_key}:repeat:{attempt}"
    text = (
        f"Напоминание {attempt}/{LIMIT}: {meal_name}. "
        "Если уже поел, пришли фото, напиши состав или «уже ел» — и я перестану напоминать про этот приём. "
        "Отложить: /позже 30 · Пропустить: /пропустить."
    )
    return Notice(key, text)
