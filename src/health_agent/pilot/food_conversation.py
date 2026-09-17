"""Small text actions that must not be treated as a new food observation."""

from __future__ import annotations

import re

_FOODS = re.compile(
    r"\b(?:каш\w*|овся\w*|рис\w*|булгур\w*|греч\w*|киноа|удон|лапш\w*|"
    r"паст\w*|макарон\w*|хлеб\w*|бублик\w*|бутерброд\w*|тост\w*|блин\w*|"
    r"яйц\w*|яич\w*|омлет\w*|куриц\w*|курин\w*|котлет\w*|говядин\w*|"
    r"рыб\w*|треск\w*|лосос\w*|тун\w*|кревет\w*|кальмар\w*|"
    r"салат\w*|овощ\w*|огур\w*|помидор\w*|томат\w*|капуст\w*|перц\w*|"
    r"яблок\w*|фрукт\w*|банан\w*|ягод\w*|миндал\w*|орех\w*|"
    r"творог\w*|йогур\w*|сыр\w*|молок\w*|масл\w*|суп\w*|"
    r"печен\w*|зефир\w*|торт\w*|вкусн\w*|шоколад\w*|кофе|чай)\b", re.IGNORECASE,
)


def food_description(text: str) -> bool:
    return bool(_FOODS.search(text))


def already_eaten(text: str) -> bool:
    return bool(re.fullmatch(
        r"(?:я\s+)?(?:же\s+)?(?:уже\s+)?"
        r"(?:ел[аи]?|поел[аи]?|покушал[аи]?|позавтракал[аи]?|пообедал[аи]?|поужинал[аи]?)"
        r"(?:\s+уже)?[.!\s]*", text.strip(), re.IGNORECASE,
    ))


def explicit_revision(text: str) -> bool:
    return bool(re.search(
        r"^(?:(?:и|а)\s+)?(?:ещ[её]\b|так\s*же\b|там\b|добав\w*\b|"
        r"это\b|не\b|убери\b|исправ\w*\b|порци\w*\b|\+)|\bэто$", text.strip(), re.IGNORECASE,
    ))


def user_calories(text: str) -> float | None:
    if not re.search(r"ккал|калори|оценил|оцениваю", text, re.IGNORECASE):
        return None
    if re.search(r"\b(?:не|меньше|больше|почему|если)\b|\?", text, re.IGNORECASE):
        return None
    match = re.fullmatch(
        r"(?:ну\s+)?(?:а\s+)?(?:так\s+)?(?:мне\s+кажется[,.]?\s+)?"
        r"(?:я\s+бы\s+оценил[аи]?\s+в|оцениваю\s+в|считай|калори[йяи]*\s*[:=]?|это)"
        r"\s*(\d{2,4})(?:[,.](\d))?\s*(?:ккал|калори[йяи]*)?[.!\s]*", text.strip(), re.IGNORECASE,
    )
    if not match:
        # A short explicit estimate can follow an explanatory sentence.
        match = re.search(r"\bя бы оценил[аи]? в\s+(\d{2,4})(?:[,.](\d))?\s*(?:ккал)?[.!\s]*$", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match[1] + ("." + match[2] if match[2] else ""))
    return value if 10 <= value <= 5000 else None


def quick_help(text: str, category: str) -> str | None:
    lowered = text.casefold()
    if re.search(r"не успева\w*|некогда (?:есть|поесть|обедать|готовить)", lowered):
        return ("Давай упростим ближайшую еду: выбери доступный готовый вариант по своему плану. "
                "Готовить специально не обязательно. Напоминание можно отложить: /позже 30.")
    if re.search(r"\bголод(?:ен|на|ный|ная|но)\b|хочется есть|хочу есть", lowered):
        return ("Если голоден, ближайший приём можно сдвинуть раньше: интервал в плане — ориентир. "
                "После еды запиши состав; это сообщение едой не записываю.")
    if re.search(r"что (?:выбрать|взять|съесть)|помоги выбрать", lowered):
        if "ужин" in lowered or category == "dinner":
            rule = "На ужин по твоему плану — белок, овощи по желанию."
        elif "завтрак" in lowered or category == "breakfast":
            rule = "На завтрак по твоему плану — долгие углеводы и фрукт, белок можно добавить."
        else:
            rule = "Для дневного приёма по плану — овощи, белок и гарнир; в обед фрукт, в полдник можно вкусняшку."
        return rule + " Выбирай доступный вариант, который удобно собрать сейчас. Запишу еду, когда сообщишь, что съел."
    return None
