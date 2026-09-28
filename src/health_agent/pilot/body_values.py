"""Deterministic, unit-aware parsing of reported scale measurements."""

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("Europe/Moscow")
FIELDS = ("weight", "fat", "muscle")
EMPTY = {"weight_kg": None, "body_fat_percent": None,
         "muscle_mass_kg": None, "muscle_percent": None}
VALUE = r"\d+(?:[.,]\d+)?"
UNIT = r"(?:кг|kg|%)"
SCALAR = re.compile(rf"({VALUE})\s*({UNIT})?", re.IGNORECASE)
LABEL = r"вес|вешу|процент\s+жира|жир|мышечная\s+масса|масса\s+мышц|мышцы"
ITEM = re.compile(rf"({LABEL})\s*[:=]?\s*({VALUE})\s*({UNIT})?", re.IGNORECASE)


def scalar(field: str, text: str) -> dict[str, float | None]:
    if text.casefold() in {"пропустить", "не знаю", "нет"}:
        return {}
    match = SCALAR.fullmatch(text.strip())
    if not match:
        raise ValueError("Напиши число с единицей или нажми «пропустить».")
    value, unit = float(match[1].replace(",", ".")), (match[2] or "").casefold()
    if field == "weight":
        if unit == "%" or not 0 < value < 500:
            raise ValueError("Вес нужен в кг: например, 76,2 кг.")
        return {"weight_kg": value}
    if field == "fat":
        if unit not in {"", "%"} or not 0 <= value <= 100:
            raise ValueError("Жир нужен в процентах: например, 20,1%.")
        return {"body_fat_percent": value}
    if not 0 < value < 500 or unit == "%" and value > 100:
        raise ValueError("Мышцы: напиши значение в кг или %, например, 57 кг.")
    return {"muscle_percent" if unit == "%" else "muscle_mass_kg": value,
            "muscle_mass_kg" if unit == "%" else "muscle_percent": None}


def labelled(text: str) -> tuple[dict[str, float | None], list[str]]:
    """Consume the entire input; never silently drop a sign or an extra field."""
    text = text.strip()
    # The app values can be dictated before their labels: "75.1 кг вес".
    text = re.sub(rf"({VALUE})\s*({UNIT})?\s+({LABEL})(?=\s*[,;]|$)",
                  lambda m: f"{m[3]} {m[1]}{m[2] or ''}", text, flags=re.IGNORECASE)
    text = re.sub(rf"^({VALUE}\s*(?:кг|kg))", r"вес \1", text, flags=re.IGNORECASE)
    values: dict[str, float | None] = {}
    answered: list[str] = []
    position = 0
    for match in ITEM.finditer(text):
        if text[position:match.start()].strip(" ,;·\n"):
            raise ValueError("Не разобрал строку. Пример: вес 76,2 кг, жир 20%, мышцы 57 кг.")
        label = match[1].casefold()
        field = "weight" if label in {"вес", "вешу"} else "muscle" if "мыш" in label else "fat"
        if field in answered:
            raise ValueError("Один показатель указан дважды. Оставь по одному значению.")
        values.update(scalar(field, match[2] + (match[3] or "")))
        answered.append(field)
        position = match.end()
    if not answered or text[position:].strip(" ,;·\n."):
        raise ValueError("Не разобрал строку. Пример: вес 76,2 кг, жир 20%, мышцы 57 кг.")
    validate(values)
    return values, answered


def validate(values: dict) -> None:
    kg, muscle = values.get("weight_kg"), values.get("muscle_mass_kg")
    if kg is not None and muscle is not None and muscle > kg:
        raise ValueError("Мышечная масса больше общего веса. Проверь число и единицу: кг или %.")


def dated(text: str, now: datetime) -> tuple[str, str]:
    """An optional leading date, otherwise the explicitly displayed current date."""
    today = now.astimezone(ZONE).date()
    text = text.strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2}|\d{2}\.\d{2}\.\d{4}|сегодня|вчера)(?:\s+|$)", text, re.IGNORECASE)
    if not match:
        return today.isoformat(), text
    token = match[1].casefold()
    try:
        day = (today if token == "сегодня" else today - timedelta(days=1) if token == "вчера"
               else date.fromisoformat("-".join(reversed(token.split(".")))) if "." in token else date.fromisoformat(token))
        if not date(1900, 1, 1) <= day <= today:
            raise ValueError
    except ValueError as error:
        raise ValueError("Нужна существующая дата не из будущего: /замер 2026-09-27.") from error
    return day.isoformat(), text[match.end():].strip()
