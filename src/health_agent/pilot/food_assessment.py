"""Short feedback for the user's four-meal plan, grounded in saved ingredients."""

from __future__ import annotations

import math
import re
from collections import Counter
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from health_agent.pilot.food_carbs import classify

LABELS = {
    "breakfast": "Завтрак",
    "lunch": "Обед",
    "afternoon": "Полдник",
    "dinner": "Ужин",
}
PLATE_RULES = {
    "breakfast": ["grains", "fruit"],
    "lunch": ["vegetables", "protein", "grains", "fruit"],
    "afternoon": ["vegetables", "protein", "grains"],
    "dinner": ["protein"],
}
_PATTERNS = {
    "vegetables": r"овощ|огур|помидор|томат|капуст|брокколи|кабач|баклаж|морков|зелень|перец|св[её]кл|листья салата|овощной салат",
    "protein": r"куриц|курин|индей|рыб|лосос|с[её]мг|треск|тун[её]ц|мяс|говяд|свин|яйц|яичн|белковый омлет|омлет|кревет|кальмар|тофу|чечевиц|фасол|\bнут\b|творог",
    "grains": r"рис|греч|овся|булгур|перлов|киноа|кускус|хлеб|паст[ауы]\b|макарон|лапш|удон|круп",
    "fruit": r"фрукт|яблок|банан|ягод|апельсин|груш|мандарин|персик|слив[аыу]|киви|виноград|малин|клубник",
    "treat": r"зефир|печень[ея]|печеньк|шоколад|конфет|торт|пирож|морожен|десерт|вкусняш|мармелад|круассан|булоч",
}


def _foods(analysis: dict[str, Any]) -> list[str]:
    foods = analysis.get("foods", [])
    return [f for f in foods if isinstance(f, str)] if isinstance(foods, list) else []


def _evidence(foods: list[str]) -> set[str]:
    result: set[str] = set()
    for food in foods:
        lower = food.casefold()
        if re.search(r"вероятно|возможно|неизвестн|\bне\b", lower):
            continue
        lower = re.split(r"\bбез\b", lower, maxsplit=1)[0]
        for key, pattern in _PATTERNS.items():
            if key == "fruit" and re.search(r"сок|джем|варень|сироп|йогурт", lower):
                continue
            if (
                key == "grains"
                and re.search(r"молоко|напиток", lower)
                and not re.search(r"каша|хлопья", lower)
            ):
                continue
            if re.search(pattern, lower):
                result.add(key)
    return result


def calories(value: Any) -> float | None:
    if (
        isinstance(value, (float, int))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    ):
        return float(value)
    return None


def _kcal(value: Any) -> str:
    number = calories(value)
    return (
        f"≈{number:,.0f} ккал".replace(",", " ")
        if number is not None
        else "калории пока неизвестны"
    )


def findings(
    analysis: dict[str, Any], category: str
) -> tuple[list[str], list[str], list[str]]:
    foods = _foods(analysis)
    evidence = _evidence(foods)
    carb_sources = classify(foods)
    whole = bool(carb_sources.get("whole_grains_legumes"))
    # A plant drink named after a grain is not a serving of slow carbohydrates.
    whole = whole and "grains" in evidence
    planned = _evidence(analysis.get("planned_additions", []))
    good: list[str] = []
    missing: list[str] = []
    notes: list[str] = []
    if category == "breakfast":
        if whole and "fruit" in evidence:
            good.append("долгие углеводы и фрукт на месте")
        else:
            if whole:
                good.append("есть долгие углеводы")
            elif "grains" not in planned:
                missing.append(
                    "долгие углеводы не подтверждены — уточни вид крупы или хлеба"
                )
            if "fruit" in evidence:
                good.append("фрукт на месте")
            elif "fruit" not in planned:
                missing.append("в записи не вижу фрукта")
        if "protein" in evidence:
            good.append("белок тоже есть — он разрешён на завтрак")
    elif category in {"lunch", "afternoon"}:
        labels = {"vegetables": "овощи", "protein": "белок", "grains": "крупа или хлеб"}
        present = [label for key, label in labels.items() if key in evidence]
        if present:
            good.append("есть " + ", ".join(present))
        absent = [
            label
            for key, label in labels.items()
            if key not in evidence and key not in planned
        ]
        if absent:
            missing.append("в записи не вижу: " + ", ".join(absent))
        if category == "lunch":
            if "fruit" in evidence:
                good.append("фрукт тоже учтён")
            elif "fruit" not in planned:
                missing.append("по твоему плану ещё фрукт — в записи его нет")
        elif "treat" in evidence:
            good.append("Вкусняшка учтена 🙂")
    elif category == "dinner":
        if "protein" in evidence:
            good.append(
                "белок на месте; овощи — по желанию"
                if "vegetables" not in evidence
                else "белок и овощи — по плану"
            )
        elif "protein" not in planned:
            missing.append("по плану нужен белок — в записи его не вижу")
        egg_foods = [
            f.casefold()
            for f in foods
            if re.search(r"яйц|яичн|омлет|желтк", f.casefold())
        ]
        for egg in egg_foods:
            if re.search(r"без желт|яичн\w* бел|белк\w* (?:яиц|омлет)|только бел", egg):
                good.append("яйца без желтков — как ты предпочитаешь")
            elif re.search(r"желтк|цел\w* яй|глазун", egg):
                missing.append("желтки записаны; на ужин ты предпочитаешь без них")
            else:
                notes.append(
                    "Яйца на ужин предпочитаешь без желтков; по записи неясно, убраны ли они."
                )
        if evidence & {"grains", "fruit"}:
            missing.append("крупа, хлеб или фрукт — вне твоего плана ужина")
    if "treat" in evidence and category != "afternoon":
        missing.append("сладость записана; по твоему плану она в полдник")
    if (
        category in {"lunch", "afternoon"}
        and {"vegetables", "protein", "grains"} <= evidence
    ):
        notes.append("Пропорции гарвардской тарелки точно не проверены.")
        if not whole:
            notes.append(
                "Для гарвардской тарелки выбирай цельную крупу; здесь её вид не подтверждён."
            )
    return good, missing, notes


def render_meal(analysis: dict[str, Any] | None, category: str) -> str:
    label = LABELS.get(category, "Приём пищи")
    if not analysis:
        return f"🍽 {label} сохранён · калории пока неизвестны\n👀 Состав пока не разобрал — оценку плана дам после разбора."
    good, missing, notes = findings(analysis, category)
    lines = [f"🍽 {label} · {_kcal(analysis.get('kcal'))}"]
    if good:
        lines.append("✅ " + "; ".join(dict.fromkeys(good)) + ".")
    if missing:
        lines.append("⚠️ " + "; ".join(dict.fromkeys(missing)) + ".")
    if not good and not missing:
        notes.append("Пока не хватает понятного состава для оценки плана.")
    lines.extend("👀 " + note for note in dict.fromkeys(notes))
    return "\n".join(lines)


def calorie_total(history: dict[str, Any]) -> str:
    meals = history["meals"]
    day = datetime.fromisoformat(history["period"]["to_date"]).strftime("%d.%m")
    if not meals:
        return f"🔥 За {day}: пока нет записей."
    values = [calories(m["nutrients_estimated"].get("kcal")) for m in meals]
    known = [v for v in values if v is not None]
    unknown = len(values) - len(known)
    categories = {m["category"] for m in meals} & LABELS.keys()
    total = _kcal(sum(known)) if known else "калории пока неизвестны"
    text = f"🔥 За {day} по записям: {total} · {len(categories)} из 4 приёмов."
    if unknown:
        text += f" Без оценки: {unknown}; сумма неполная."
    if history.get("truncated"):
        text += " Показана только часть записей."
    return text


def daily_summary(history: dict[str, Any]) -> str:
    lines = ["📊 Итог дня", calorie_total(history)]
    meals = history["meals"]
    for category, label in LABELS.items():
        selected = [m for m in meals if m["category"] == category]
        if not selected:
            lines.append(f"▫️ {label}: нет записи")
            continue
        values = [calories(m["nutrients_estimated"].get("kcal")) for m in selected]
        known = [v for v in values if v is not None]
        text = _kcal(sum(known)) if known else "калории пока неизвестны"
        if known and len(known) < len(values):
            text += " + есть еда без оценки"
        lines.append(f"• {label}: {text}")
    return "\n".join(lines)


def plan_text(protocol: dict[str, Any]) -> str:
    text = (
        "🥣 Завтрак: долгие углеводы + фрукт; белок можно.\n"
        "🍽 Обед: гарвардская тарелка + фрукт.\n"
        "🍪 Полдник: гарвардская тарелка + вкусняшка.\n"
        "🐟 Ужин: белок; яйца желательно без желтков, овощи — по желанию.\n"
        "Тарелка: примерно ½ овощей, ¼ белка, ¼ цельной крупы.\n"
        "🔥 Считаем калории; дневного лимита пока нет."
    )
    focus = protocol.get("long_term_focus")
    target = (
        calories(focus.get("desired_fat_loss_kg")) if isinstance(focus, dict) else None
    )
    if (
        isinstance(focus, dict)
        and target is not None
        and target > 0
        and focus.get("timeframe", "one_month") == "one_month"
    ):
        text += (
            f"\n🎯 Фокус: снизить жировую массу. Желаемый ориентир — {target:g} кг за месяц, не обещание результата."
            "\nСледим за порциями, калориями и тенденцией веса за неделю. Изменение веса не равно потере жира."
        )
    return text


def weekly_summary(history: dict[str, Any]) -> str:
    meals = history["meals"]
    period = history["period"]
    start = datetime.fromisoformat(period["from_date"]).strftime("%d.%m")
    end = datetime.fromisoformat(period["to_date"]).strftime("%d.%m")
    if not meals:
        return f"📊 Неделя {start}–{end}: пока нет записей."
    days: dict[str, set[str]] = {}
    issues: Counter[str] = Counter()
    successes: Counter[str] = Counter()
    examined: Counter[str] = Counter()
    for meal in meals:
        day = (
            datetime.fromisoformat(meal["recorded_at"])
            .astimezone(ZoneInfo("Europe/Moscow"))
            .date()
            .isoformat()
        )
        category = meal["category"]
        days.setdefault(day, set()).add(category)
        if meal["analysis_available"] and meal["foods"]:
            good, missing, notes = findings({"foods": meal["foods"]}, category)
            if category in LABELS:
                examined[category] += 1
                if (
                    good
                    and not missing
                    and not any("не подтверждён" in n or "неясно" in n for n in notes)
                ):
                    successes[category] += 1
            issues.update(dict.fromkeys(missing, 1))
    complete_days = sum(LABELS.keys() <= categories for categories in days.values())
    values = [calories(m["nutrients_estimated"].get("kcal")) for m in meals]
    known = [value for value in values if value is not None]
    unknown = len(values) - len(known)
    total = _kcal(sum(known)) if known else "калории пока неизвестны"
    lines = [
        f"📊 Неделя {start}–{end}",
        f"📝 Записано {len(meals)} приёмов за {len(days)} из {period['days']} дней; все 4 приёма есть в {complete_days} дн.",
        f"🔥 По записям: {total}."
        + (f" Без оценки: {unknown}; сумма неполная." if unknown else ""),
    ]
    matched = [
        f"{LABELS[c].lower()} {successes[c]}/{examined[c]}"
        for c in LABELS
        if examined[c]
    ]
    lines.append(
        "✅ По текущему плану, без явных замечаний по составу: "
        + ", ".join(matched)
        + "."
        if matched
        else "👀 Состав пока не разобран — сравнить с планом не получается."
    )
    if issues:
        top = issues.most_common(2)
        lines.append(
            "⚠️ Повторялось: "
            + "; ".join(f"{issue} ({count} раз)" for issue, count in top)
            + "."
        )
    else:
        lines.append(
            "🙂 Явных отклонений в разобранных записях не нашёл. Вкусняшка в полдник разрешена."
        )
    if complete_days < period["days"] or history.get("truncated"):
        lines.append("👀 Журнал неполный; это не калории всего рациона за неделю.")
    if unknown:
        focus = "указывай размер порции, чтобы закрыть пробелы в калориях"
    elif issues:
        top_issue = issues.most_common(1)[0][0]
        if "фрукт" in top_issue and "вне" not in top_issue:
            focus = "добавляй и записывай фрукт к завтраку и обеду"
        elif "белок" in top_issue:
            focus = "добавляй и записывай источник белка в дневные тарелки и ужин"
        elif "желтки" in top_issue:
            focus = "для яиц на ужин выбирай белки, как ты и хотел"
        elif "сладость" in top_issue:
            focus = "оставь запланированную вкусняшку на полдник"
        else:
            focus = "сверяй состав с /план перед едой и записывай дополнения"
    else:
        focus = "записывай масло и соусы вместе с блюдом; вкусняшку в полдник оставляем"
    lines.append("🎯 На следующую неделю: " + focus + ".")
    lines.append(
        "Потерю жира по дневнику еды не определить; дефицит пока не подтверждён."
    )
    return "\n".join(lines)
