"""Ingredient-grounded carbohydrate sources; no invented GI or sugar grams."""

from __future__ import annotations

import re
from typing import Any

PATTERNS = {
    "whole_grains_legumes": r"цельнозерн|греч|овсян|киноа|булгур|перлов|бурый рис|чечевиц|фасол|\bнут\b|whole.?grain",
    "refined_starch": r"белый хлеб|белый рис|батон\b|блин|булоч|сдоб|white bread",
    "free_sugars": r"сахар|сироп|м[её]д\b|конфет|карамел|сладк\w* напит|лимонад|(?:фруктов\w* |яблочн\w* |апельсинов\w* )?сок\b",
    "whole_fruit": r"яблок|банан|ягод|апельсин|груш|мандарин",
    "unspecified_starch": r"каш|хлеб|рис\b|макарон|паст[ауы]\b|картоф",
}
LABELS = {
    "whole_grains_legumes": "сложные углеводы: крупы/цельное зерно/бобовые",
    "refined_starch": "рафинированный крахмал",
    "free_sugars": "свободные сахара (быстрые углеводы)",
    "whole_fruit": "цельные фрукты/ягоды",
    "unspecified_starch": "крахмал, вид продукта требует уточнения",
}


def classify(foods: Any) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for food in foods if isinstance(foods, list) else []:
        if not isinstance(food, str):
            continue
        lower = food.casefold()
        if "вероятно" in lower or "возможно" in lower:
            continue
        matched = {
            key for key, pattern in PATTERNS.items() if re.search(pattern, lower)
        }
        if re.search(r"без\s+сахара|несладк", lower):
            matched.discard("free_sugars")
        if "сок" in lower:
            matched.discard("whole_fruit")
        if matched & {"whole_grains_legumes", "refined_starch"}:
            matched.discard("unspecified_starch")
        for key in PATTERNS:
            if key in matched:
                result.setdefault(key, []).append(food[:200])
    return result


def feedback(foods: Any) -> str:
    categories = classify(foods)
    if not categories:
        return ""
    return "Углеводы: " + "; ".join(LABELS[k] for k in categories) + "."


def weekly(history: dict[str, Any]) -> str:
    meals = history["meals"]
    count = len(meals)
    sugars = sum("free_sugars" in classify(m["foods"]) for m in meals)
    refined = sum("refined_starch" in classify(m["foods"]) for m in meals)
    if sugars:
        return (
            "На следующую неделю: в одном привычном сладком перекусе замени сладость на цельный фрукт. "
            f"Основание: источники свободных сахаров отмечены в {sugars} из {count} записанных приёмов. "
            "Это вывод по журналу, не по полному рациону."
        )
    if refined:
        return (
            "На следующую неделю: в одном обычном приёме замени белый хлеб или выпечку на цельнозерновой хлеб. "
            f"Основание: рафинированные крахмалистые продукты отмечены в {refined} из {count} записанных приёмов. "
            "Это вывод по журналу, не по полному рациону."
        )
    unknown = sum(
        not m["analysis_available"] or "unspecified_starch" in classify(m["foods"])
        for m in meals
    )
    if unknown:
        return (
            "На следующую неделю: указывай вид крупы или хлеба в подписи к фото. "
            f"В {unknown} из {count} записей состав не разобран или вид крахмалистого продукта неясен; "
            "пока это мешает выбрать обоснованное изменение еды."
        )
    return (
        f"За неделю записей: {count}. По этим данным убедительной причины менять рацион пока нет. "
        "Один шаг: продолжай записывать все приёмы ещё неделю, чтобы проверить повторяющийся паттерн."
    )
