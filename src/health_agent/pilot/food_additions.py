"""Preserve explicit short addition statements independently of vision output."""

from __future__ import annotations

import re
from typing import Any

_PREFIX = r"(?:(?:я|ещ[её]|уже|тогда|в итоге)\s+)*"
_VERB = r"добавлю|добавил[аи]?|добавила|добавляю"
_UNCERTAIN = re.compile(
    r"\?|\b(?:если|может|наверное|завтра|потом|почему|можно|нужно|надо)\b"
)


_FOOD = re.compile(
    r"\b(?:зефир\w*|печен\w*|хлеб\w*|сыр\w*|масл\w*|яблок\w*|банан\w*|"
    r"орех\w*|миндал\w*|кофе|чай|молок\w*|йогур\w*|кефир\w*|творог\w*|"
    r"каш\w*|рис\w*|булгур\w*|греч\w*|киноа|кускус\w*|салат\w*|овощ\w*|"
    r"огур\w*|помидор\w*|томат\w*|морков\w*|капуст\w*|яйц\w*|куриц\w*|"
    r"рыб\w*|говядин\w*|кревет\w*|суп\w*|шоколад\w*|конфет\w*|торт\w*|"
    r"пирог\w*|крекер\w*|булоч\w*|блин\w*|сахар\w*|м[её]д|сок\w*|ягод\w*)\b"
)
_INTENTION = re.compile(
    r"\b(?:хочу|буду|планирую|собираюсь|думаю|добавлю|съем|съесть|поем|куплю|купить|может|пожалуй)\b"
)
_GRAIN = re.compile(r"\b(?:рис|булгур|греч\w*|киноа|кускус|перлов\w*|овсян\w*)\b")


def _words(text: str) -> list[str]:
    words = re.findall(r"[а-яёa-z]+", text.casefold())
    result = []
    for word in words:
        if word in {"и", "с", "из", "на"}:
            continue
        if word.startswith("зефирк"):
            word = "зефир"
        elif word.startswith("печеньк") or word in {"печенье", "печенья"}:
            word = "печенье"
        result.append(word)
    return result


def _parts(food: str) -> list[str]:
    # Keep composed dishes intact (e.g. salad made of cucumbers and tomatoes).
    if re.search(r"\b(?:с|из)\b", food):
        return [food]
    return [part.strip() for part in re.split(r"\s+и\s+|,", food) if part.strip()]


def grain_corrections(texts: list[str], known_foods: list[str]) -> list[dict[str, str]]:
    known = [
        f
        for f in known_foods
        if _GRAIN.search(f.casefold()) and not re.search(r"\b(?:с|из)\b", f.casefold())
    ]
    if len(known) != 1:
        return []
    result = []
    for text in texts:
        value = text.strip().casefold().rstrip(".!")
        match = re.fullmatch(r"(?:это (.+)|(.+) это)", value)
        if not match:
            continue
        food = (match[1] or match[2]).strip()
        if _GRAIN.fullmatch(food):
            result = [{"replacement": food, "replaces": known[0]}]
    return result


def correct_grains(
    analysis: dict[str, Any],
    corrections: list[dict[str, str]],
    nutrients: tuple[str, ...],
) -> dict[str, Any]:
    result = dict(analysis)
    for correction in corrections:
        replacement = correction["replacement"]
        foods = result["foods"]
        wrong = [
            f
            for f in foods
            if _GRAIN.search(f.casefold())
            and not mentions(replacement, f)
            and not re.search(r"\b(?:с|из)\b", f.casefold())
        ]
        missing = not any(mentions(replacement, f) for f in foods)
        if wrong or missing:
            result["foods"] = [f for f in foods if f not in wrong]
            if missing:
                result["foods"].append(replacement)
            for key in nutrients:
                result[key] = None
            result["unknowns"] = [
                *result.get("unknowns", []),
                "Вид крупы исправлен по сообщению; нутриенты требуют пересчёта.",
            ]
    return result


def statement(text: str) -> tuple[str, str] | None:
    value = text.strip().lower().rstrip(".!").strip()
    value = re.sub(r"^(?:вот\s+)?(?:обед|ужин|завтрак)[,:]\s*", "", value)
    if _UNCERTAIN.search(value) or len(value) > 140:
        return None
    cancel = re.fullmatch(rf"{_PREFIX}не\s+(?:{_VERB})\s+(.+)", value)
    cancel = cancel or re.fullmatch(rf"{_PREFIX}(.+?)\s+не\s+(?:{_VERB})", value)
    if cancel:
        return cancel.group(1).strip(), "cancelled"
    if re.search(r"\bне\b", value):
        return None
    match = re.fullmatch(rf"{_PREFIX}(?P<verb>{_VERB})\s+(?P<food>.+)", value)
    match = match or re.fullmatch(rf"{_PREFIX}(?P<food>.+?)\s+(?P<verb>{_VERB})", value)
    if match:
        food = re.sub(rf"^{_PREFIX}", "", match["food"]).strip()
        if food and len(food) <= 100:
            return food, "planned" if match["verb"] == "добавлю" else "confirmed"
    short = re.fullmatch(r"(?:(?:(?:и|а)\s+)?ещ[её]\s+|\+\s*)(.+)", value)
    if (
        short
        and not _INTENTION.search(value)
        and not re.search(
            r"\b(?:о|об|про|расскажи|напомни|покажи|спроси|посчитай)\b", value
        )
    ):
        food = short[1].strip()
        if len(food) <= 100 and all(_FOOD.search(part) for part in _parts(food)):
            return food, "confirmed"
    return None


def mentions(label: str, description: str) -> bool:
    """Allow a named food in a longer portion description, e.g. хлеб → ломтик хлеба."""
    words = _words(label)
    description = " ".join(_words(description))
    return bool(words) and all(
        re.search(r"\b" + re.escape(word) + r"\w*\b", description) for word in words
    )


def additions(texts: list[str]) -> dict[str, list[str]]:
    planned: list[str] = []
    confirmed: list[str] = []
    excluded: list[str] = []
    parsed_items: list[tuple[str, str]] = []
    for text in texts:
        parsed = statement(text)
        if parsed is not None:
            food, status = parsed
            parsed_items.extend((part, status) for part in _parts(food))
    for food, status in parsed_items:
        if status == "cancelled":
            planned = [
                f for f in planned if not (mentions(f, food) or mentions(food, f))
            ]
            confirmed = [
                f for f in confirmed if not (mentions(f, food) or mentions(food, f))
            ]
            excluded.append(food)
        else:
            excluded = [
                f for f in excluded if not (mentions(f, food) or mentions(food, f))
            ]
            if status == "confirmed":
                planned = [
                    f for f in planned if not (mentions(f, food) or mentions(food, f))
                ]
                if not any(mentions(food, f) or mentions(f, food) for f in confirmed):
                    confirmed.append(food)
            elif not any(mentions(food, f) for f in confirmed) and food not in planned:
                planned.append(food)
    return {
        "planned_additions": planned,
        "confirmed_additions": confirmed,
        "excluded_additions": excluded,
    }


def reconcile(
    analysis: dict[str, Any],
    evidence: dict[str, list[str]],
    previous: dict[str, Any] | None,
    nutrients: tuple[str, ...],
) -> dict[str, Any]:
    result = dict(analysis)
    foods = list(result["foods"])
    original_foods = (previous or {}).get("foods", [])
    removed = [
        f
        for f in foods
        if any(mentions(x, f) for x in evidence["excluded_additions"])
        or (
            any(mentions(x, f) for x in evidence["planned_additions"])
            and not any(mentions(f, original) for original in original_foods)
            and not any(mentions(x, f) for x in evidence["confirmed_additions"])
        )
    ]
    foods = [f for f in foods if f not in removed]
    missing = [
        f
        for f in evidence["confirmed_additions"]
        if not any(mentions(f, x) for x in foods)
    ]
    result["foods"] = [*foods, *missing]
    if removed or missing:
        for key in nutrients:
            result[key] = None
        result["unknowns"] = [
            *result.get("unknowns", []),
            "Состав уточнён по сообщению; нутриенты требуют пересчёта с учётом дополнений.",
        ]
    return result
