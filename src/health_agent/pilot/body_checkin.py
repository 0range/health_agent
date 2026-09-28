"""Three-question manual scale check-in with durable review and replay."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID

from health_agent.pilot.body_values import (
    EMPTY,
    FIELDS,
    SCALAR,
    ZONE,
    dated,
    labelled,
    scalar,
    validate,
)
from health_agent.pilot.contracts import Notice, Record, Store

BUTTON = re.compile(r"^Замер №(\d+): (сохранить|исправить|отмена|пропустить (?:вес|жир|мышцы))$")
START = re.compile(r"^(?:/замер|замер|начать замер)(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
REMINDER = "⚖️ Утренний замер"
LABELS = {"weight": "вес", "fat": "жир", "muscle": "мышцы"}
PROMPTS = {
    "weight": "1/3 · Какой вес в кг? Например, 76,2.",
    "fat": "2/3 · Какой процент жира показывают весы? Например, 20,1%.",
    "muscle": "3/3 · Какая мышечная масса в кг? Если на весах %, напиши число с %.",
}


def keyboard(text: str) -> dict[str, Any] | None:
    if text.startswith(REMINDER):
        return {"keyboard": [["Начать замер"]], "resize_keyboard": True, "one_time_keyboard": True}
    match = re.search(r"⚖️ Замер №(\d+)", text)
    if match:
        prefix = f"Замер №{match[1]}: "
        if "Проверь" in text:
            buttons = [[prefix + "сохранить"], [prefix + "исправить", prefix + "отмена"]]
        else:
            field = next((k for k, prompt in PROMPTS.items() if prompt in text), None)
            if field is None:
                return None
            buttons = [[prefix + "пропустить " + LABELS[field]], [prefix + "отмена"]]
        return {"keyboard": buttons, "resize_keyboard": True, "one_time_keyboard": True}
    if text.startswith(("Замер сохранён", "Замер отменён", "Все показатели пропущены")):
        return {"remove_keyboard": True}
    return None


def _summary(payload: dict[str, Any]) -> str:
    def value(field: str, unit: str):
        number = payload.get(field)
        return "пропущено" if number is None else f"{number:g} {unit}"

    muscle = (value("muscle_percent", "%") if payload.get("muscle_percent") is not None
              else value("muscle_mass_kg", "кг"))
    return (f"Вес: {value('weight_kg', 'кг')}\nЖир: {value('body_fat_percent', '%')}\n"
            f"Мышцы: {muscle}")


def _prompt(draft: Record) -> str:
    p = draft.payload
    day = date.fromisoformat(p["measurement_date"])
    header = f"⚖️ Замер №{p['number']} за {day:%d.%m.%Y}\n"
    field = next((f for f in FIELDS if f not in p["answered"]), None)
    if field is not None:
        return header + PROMPTS[field] + "\nНе знаешь — можно пропустить."
    return header + "Проверь перед сохранением:\n" + _summary(p) + "\nЖир и мышцы — оценки весов."


def _remember(store: Store, profile: UUID, draft: Record, payload: dict, key: str, text: str) -> Record:
    return store.patch(profile, draft.id, {**payload,
                       "handled": {**draft.payload.get("handled", {}), key: text}})


def _save(store: Store, profile: UUID, draft: Record, now: datetime, input_key: str) -> str:
    p = draft.payload
    if len(p["answered"]) != 3:
        return _prompt(draft)
    if all(p.get(k) is None for k in EMPTY):
        reply = "Все показатели пропущены — пустой замер не сохраняю."
        _remember(store, profile, draft, {**p, "status": "cancelled"}, input_key, reply)
        return reply
    measured = datetime.combine(date.fromisoformat(p["measurement_date"]), time.min, ZONE)
    key = "body:" + draft.source_key
    record = store.put(profile, "shared", "body_measurement", key, {
        **{k: p.get(k) for k in EMPTY}, "measurement_date": p["measurement_date"],
        "timestamp_precision": "day", "source": "user_report_scale",
        "recorded_at": now.isoformat(), "schema_version": 1,
    }, at=measured)
    # Replay finishes a partial save before marking the draft completed.
    if record.payload["weight_kg"] is not None:
        store.put(profile, "shared", "weight", key,
                  {**record.payload, "body_measurement_id": record.id}, at=record.at)
    reply = f"Замер сохранён за {measured:%d.%m.%Y}.\n" + _summary(record.payload)
    reply += "\nОн доступен в общем разборе /инсайты."
    _remember(store, profile, draft, {**p, "status": "saved", "reply": reply,
                                    "measurement_id": record.id}, input_key, reply)
    return reply


def handle(store: Store, profile: UUID, text: str, key: str, now: datetime) -> str | None:
    if now.tzinfo is None:
        raise ValueError("now_requires_timezone")
    cached = store.by_source(profile, "shared", "body_reply", key)
    if cached is not None:
        return str(cached.payload["text"])

    def reply(value: str) -> str:
        return str(store.put(profile, "shared", "body_reply", key, {"text": value}, at=now).payload["text"])

    text = text.strip()
    if text.casefold() in {"/замер напоминания вкл", "/замер напоминания выкл"}:
        enabled = text.casefold().endswith("вкл")
        setting = store.by_source(profile, "shared", "settings", "body-checkin")
        payload: dict[str, Any] = {"reminders_enabled": enabled, "weekdays": [3, 6], "local_time": "08:00",
                   "timezone": "Europe/Moscow", "updated_at": now.isoformat()}
        if setting is None:
            store.put(profile, "shared", "settings", "body-checkin", payload, at=now)
        else:
            store.patch(profile, setting.id, payload)
        return reply("Опрос весов: четверг и воскресенье, 08:00 по Москве." if enabled
                     else "Напоминания о замерах выключены. Записать замер можно командой /замер.")
    start, button = START.fullmatch(text), BUTTON.fullmatch(text)
    rows = store.list(profile, "shared", "body_checkin", limit=100)
    for row in rows:
        if key in row.payload.get("handled", {}):
            return reply(row.payload["handled"][key])
    latest = max(rows, key=lambda r: r.payload["number"], default=None)
    draft = latest if latest is not None and latest.payload["status"] == "active" else None
    if draft is not None and now - draft.at > timedelta(hours=24):
        store.patch(profile, draft.id, {**draft.payload, "status": "expired"})
        draft = None
        if button:
            return reply("Срок замера истёк. Начни новый: /замер.")

    if button:
        if latest is None:
            return reply("Нет замера для этой кнопки. Начни: /замер.")
        if int(button[1]) != latest.payload["number"]:
            return reply("Эта кнопка устарела. Продолжить текущий замер: /замер.")
        if latest.payload["status"] == "saved":
            return reply(str(latest.payload["reply"]))
        if draft is None:
            return reply("Нет активного замера. Начни: /замер.")

    action = button[2] if button else ((start[1] or "").casefold() if start else "")
    if action in {"отмена", "исправить", "сохранить"}:
        if draft is None:
            return reply("Нет активного замера. Начни: /замер.")
        if action == "отмена":
            result = "Замер отменён. Для нового: /замер."
            _remember(store, profile, draft, {**draft.payload, "status": "cancelled"}, key, result)
            return reply(result)
        if action == "исправить":
            payload = {**draft.payload, **EMPTY, "answered": []}
            result = _prompt(replace(draft, payload=payload))
            _remember(store, profile, draft, payload, key, result)
            return reply(result)
        return reply(_save(store, profile, draft, now, key))

    inline = bool(re.search(r"\b(?:жир|мышцы|мышечная\s+масса|масса\s+мышц)\b", text, re.IGNORECASE)
                  and re.match(r"^(?:вес\b|вешу\b|\d+[.,]?\d*\s*(?:кг|kg)\b)", text, re.IGNORECASE))
    if start is not None or inline:
        argument = (start[1] or "") if start else text
        if not argument and draft is not None:
            return reply(_prompt(draft))
        try:
            day, argument = dated(argument, now)
            values, answered = labelled(argument) if argument else ({}, [])
        except ValueError as error:
            return reply(str(error))
        if draft is not None:
            return reply("Есть незавершённый замер. Продолжи /замер или отмени: /замер отмена.")
        number = (latest.payload["number"] if latest else 0) + 1
        payload = {
            **EMPTY, **values, "answered": answered, "measurement_date": day,
            "status": "active", "number": number,
        }
        result = _prompt(Record("", "shared", "body_checkin", key, now, payload))
        store.put(profile, "shared", "body_checkin", key,
                  {**payload, "handled": {key: result}}, at=now)
        return reply(result)

    if draft is None:
        return None
    field = next((f for f in FIELDS if f not in draft.payload["answered"]), None)
    if field is None:
        return None
    if button and button[2] != "пропустить " + LABELS[field]:
        return reply(_prompt(draft))
    answer = "пропустить" if button else text.casefold()
    if not (SCALAR.fullmatch(answer) or answer in {"пропустить", "нет", "не знаю"}):
        return None
    try:
        values = scalar(field, answer)
        payload = {**draft.payload, **values, "answered": [*draft.payload["answered"], field]}
        validate(payload)
    except ValueError as error:
        return reply(str(error) + "\n" + _prompt(draft))
    result = _prompt(replace(draft, payload=payload))
    if len(payload["answered"]) == 3 and all(payload.get(k) is None for k in EMPTY):
        return reply(_save(store, profile, replace(draft, payload=payload), now, key))
    _remember(store, profile, draft, payload, key, result)
    return reply(result)


def due(store: Store, profile: UUID, now: datetime) -> list[Notice]:
    """One morning reminder; existing outbound delivery owns durable receipts."""
    setting = store.by_source(profile, "shared", "settings", "body-checkin")
    if setting is None or setting.payload.get("reminders_enabled") is not True:
        return []
    local = now.astimezone(ZONE)
    if local.weekday() not in (3, 6) or not time(8) <= local.time() < time(12):
        return []
    day = local.date().isoformat()
    if any(r.payload.get("measurement_date") == day and r.at <= now
           for r in store.list(profile, "shared", "body_measurement", limit=100)):
        return []
    key = "body:reminder:" + day
    if store.by_source(profile, "sleep", "notice", key) is not None:
        return []
    return [Notice(key, f"{REMINDER} · {local:%d.%m}\nЕсли взвесился, запишем вес, процент жира и мышцы. "
                   "Три коротких вопроса; неизвестное можно пропустить.\nНачни кнопкой или /замер.")]
