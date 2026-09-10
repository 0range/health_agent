"""Preserve explicit short addition statements independently of vision output."""

from __future__ import annotations

import re
from typing import Any

_PREFIX = r"(?:(?:я|ещ[её]|уже|тогда|в итоге)\s+)*"
_VERB = r"добавлю|добавил[аи]?|добавила|добавляю"
_UNCERTAIN = re.compile(
    r"\?|\b(?:если|может|наверное|завтра|потом|почему|можно|нужно|надо)\b"
)


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
    return None


def mentions(label: str, description: str) -> bool:
    """Allow a named food in a longer portion description, e.g. хлеб → ломтик хлеба."""
    words = re.findall(r"[а-яёa-z]+", label.casefold())
    return bool(words) and all(
        re.search(r"\b" + re.escape(word) + r"\w*\b", description.casefold())
        for word in words
    )


def additions(texts: list[str]) -> dict[str, list[str]]:
    planned: list[str] = []
    confirmed: list[str] = []
    excluded: list[str] = []
    for text in texts:
        parsed = statement(text)
        if parsed is None:
            continue
        food, status = parsed
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
                if food not in confirmed:
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
