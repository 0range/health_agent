"""Two-tap subjective diary, adapted concepts rather than a validated scale."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Store

ZONE = ZoneInfo("Europe/Moscow")
QUALITY = "1/2 · Как спалось в целом?\n0 — очень плохо, 10 — отлично."
RESTED = "2/2 · Насколько чувствуешь себя выспавшимся сейчас?\n0 — совсем не выспался, 10 — полностью выспался."
PROMPT = "Доброе утро! Два коротких вопроса о прошедшей ночи.\n" + QUALITY
FINISHED = "Оценки сна сохранены."
BUTTON = re.compile(r"^(Сон|Выспался): (10|[0-9]|пропустить)$")


def keyboard(text: str) -> dict[str, Any] | None:
    label = "Сон" if QUALITY in text else "Выспался" if RESTED in text else None
    if label:
        buttons = [f"{label}: {n}" for n in range(11)]
        return {
            "keyboard": [
                buttons[:4],
                buttons[4:8],
                buttons[8:],
                [f"{label}: пропустить"],
            ],
            "resize_keyboard": True,
            "one_time_keyboard": True,
            "input_field_placeholder": "Выбери оценку кнопкой",
        }
    if text.startswith(FINISHED):
        return {"remove_keyboard": True}
    return None


def _draft(store: Store, profile: UUID, day: str):
    return next(
        (
            r
            for r in store.list(profile, "sleep", "checkin", limit=100)
            if r.source_key == day
        ),
        None,
    )


def completed(store: Store, profile: UUID, now: datetime) -> bool:
    draft = _draft(store, profile, now.astimezone(ZONE).date().isoformat())
    return draft is not None and "rested_0_10" in draft.payload


def start(store: Store, profile: UUID, now: datetime) -> str:
    day = now.astimezone(ZONE).date().isoformat()
    draft = _draft(store, profile, day)
    if draft is None:
        draft = store.put(
            profile,
            "sleep",
            "checkin",
            day,
            {"schema_version": 2, "wake_date": day},
            at=now,
        )
    if "rested_0_10" in draft.payload:
        return _finish(store, profile, draft.payload, now)
    return RESTED if "quality_0_10" in draft.payload else PROMPT


def handle(store: Store, profile: UUID, text: str, now: datetime) -> str | None:
    if text.casefold() == "/опрос":
        return start(store, profile, now)
    match = BUTTON.fullmatch(text)
    if match is None:
        return None
    day = now.astimezone(ZONE).date().isoformat()
    draft = _draft(store, profile, day)
    if draft is None:
        return "Для сегодняшнего опроса нажми /опрос."
    payload = dict(draft.payload)
    if "rested_0_10" in payload:
        return _finish(store, profile, payload, now)
    field = "quality_0_10" if match[1] == "Сон" else "rested_0_10"
    expected = "rested_0_10" if "quality_0_10" in payload else "quality_0_10"
    if field != expected:
        return RESTED if expected == "rested_0_10" else PROMPT
    payload[field] = None if match[2] == "пропустить" else int(match[2])
    payload[field + "_answered_at"] = now.isoformat()
    store.patch(profile, draft.id, payload)
    return _finish(store, profile, payload, now) if field == "rested_0_10" else RESTED


def _finish(store: Store, profile: UUID, payload: dict[str, Any], now: datetime) -> str:
    def score(key: str) -> str:
        return "пропущено" if payload[key] is None else f"{payload[key]}/10"

    text = f"Качество сна: {score('quality_0_10')}; выспался: {score('rested_0_10')}."
    store.put(
        profile,
        "sleep",
        "diary",
        "checkin:" + payload["wake_date"],
        {
            "text": text,
            "checkin": payload,
            "morning_prompt_key": "morning:" + payload["wake_date"],
        },
        at=now,
    )
    return (
        FINISHED
        + " "
        + text
        + " Если было что-то необычное, можно добавить: /сон и комментарий."
    )
