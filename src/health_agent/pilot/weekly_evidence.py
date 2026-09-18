"""Period-scoped recorded facts for the shared coaching loop."""

from datetime import date, datetime, time, timedelta
from statistics import mean
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Store
from health_agent.pilot.food_history import build_food_history

ZONE = ZoneInfo("Europe/Moscow")


def evidence(
    store: Store, profile: UUID, first: date, last: date, now: datetime
) -> dict[str, Any]:
    end = min(
        now,
        datetime.combine(last + timedelta(days=1), time.min, ZONE)
        - timedelta(microseconds=1),
    )
    if end.astimezone(ZONE).date() < first:
        return {
            "not_started": True,
            "from_date": first.isoformat(),
            "through_date": last.isoformat(),
        }
    history = build_food_history(
        store, profile, end, days=(end.astimezone(ZONE).date() - first).days + 1
    )
    activity_ids: set[str] = set()
    coros_days: set[date] = set()
    for row in store.list(profile, "training", "activity", limit=1000):
        day = row.at.astimezone(ZONE).date()
        if first <= day <= last and row.at <= now:
            activity_ids.add(str(row.payload.get("id") or row.source_key))
            coros_days.add(day)
    manual_days: set[date] = set()
    for row in store.list(profile, "training", "manual_activity", limit=1000):
        try:
            day = date.fromisoformat(row.payload["date"])
        except (KeyError, TypeError, ValueError):
            continue
        if first <= day <= last and row.at <= now:
            manual_days.add(day)
    slept: dict[str, float] = {}
    for row in store.list(profile, "sleep", "diary", limit=1000):
        checkin = row.payload.get("checkin", {})
        try:
            day = date.fromisoformat(checkin["wake_date"])
        except (KeyError, TypeError, ValueError):
            continue
        score = checkin.get("rested_0_10")
        if (
            first <= day <= last
            and row.at <= now
            and isinstance(score, (int, float))
            and not isinstance(score, bool)
            and 0 <= score <= 10
        ):
            slept.setdefault(day.isoformat(), float(score))
    weights = []
    for row in store.list(profile, "shared", "weight", limit=1000):
        value = row.payload.get("weight_kg")
        if (
            row.at <= now
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and 0 < value < 500
        ):
            weights.append(
                {
                    "date": row.at.astimezone(ZONE).date().isoformat(),
                    "kg": value,
                    "source": row.payload.get("source", "unknown"),
                }
            )
    weights.sort(key=lambda r: r["date"], reverse=True)
    latest = weights[0] if weights else None
    period_weights = [
        w for w in weights if first.isoformat() <= w["date"] <= last.isoformat()
    ]
    syncs = store.list(profile, "training", "sync_run", limit=1)
    return {
        "from_date": first.isoformat(),
        "through_date": last.isoformat(),
        "as_of": end.isoformat(),
        "meal_count": history["recorded_meal_count"],
        "food_days": len(history["recorded_days"]),
        "food_truncated": history["truncated"],
        "coros_sessions": len(activity_ids),
        "manual_days_without_coros": len(manual_days - coros_days),
        "training_count": len(activity_ids) + len(manual_days - coros_days),
        "possible_manual_overlap": bool(manual_days & coros_days),
        "training_last_sync": syncs[0].at.isoformat() if syncs else None,
        "training_sync_status": syncs[0].payload.get("status") if syncs else None,
        "rested_mean": round(mean(slept.values()), 1) if slept else None,
        "rested_days": len(slept),
        "latest_weight": latest,
        "period_weight_days": len({w["date"] for w in period_weights}),
        "weight_stale": latest is None
        or (now.astimezone(ZONE).date() - date.fromisoformat(latest["date"])).days > 7,
        "limits": "Logged facts only; absent entries are unknown. Weight does not measure fat loss. Manual sessions count at most once per day and never on a COROS day.",
    }


def report_text(
    facts: dict[str, Any], plan: dict[str, Any], result: str | None = None
) -> str:
    if facts.get("not_started"):
        return "Эта неделя ещё не началась; выполнение пока не оцениваю."
    first, last = (
        date.fromisoformat(facts["from_date"]),
        date.fromisoformat(facts["through_date"]),
    )
    lines = [
        f"Итог общей недели {first:%d.%m}–{last:%d.%m} · по записям на {datetime.fromisoformat(facts['as_of']).astimezone(ZONE):%d.%m %H:%M}."
    ]
    count, target = facts["training_count"], plan["training_sessions"]
    lines.append(
        f"🏃 Записано занятий: {count}; согласованный минимум — {target}. "
        + (
            "Минимум по записям выполнен."
            if count >= target
            else "Остальное выполнение пока не подтверждено."
        )
    )
    if facts["possible_manual_overlap"]:
        lines.append("Ручную запись в день COROS повторно не прибавлял.")
    lines.append(
        f"🍽 Еда: {facts['meal_count']} записей за {facts['food_days']} дн. Фокус: {plan['food_focus']}"
    )
    lines.append(
        f"Твой итог: {result}"
        if result
        else "Результат по пищевому фокусу пока не подтверждён."
    )
    if facts["rested_mean"] is not None:
        lines.append(
            f"😴 Выспался в среднем на {facts['rested_mean']}/10; ответов: {facts['rested_days']}."
        )
    else:
        lines.append("😴 Свежих оценок самочувствия за эту неделю нет.")
    weight = facts["latest_weight"]
    lines.append(
        f"⚖️ Последний вес: {weight['kg']:g} кг, {date.fromisoformat(weight['date']):%d.%m}."
        if weight
        else "⚖️ Записи веса пока нет."
    )
    if facts["weight_stale"]:
        step = "при возможности записать свежий вес: /вес <кг>. По старому измерению результат цели не оцениваю."
    elif result:
        step = (
            "учесть твою трудность в следующем черновике; изменение нужно подтвердить."
        )
    else:
        step = "одной фразой отметить, что получилось или мешало: /цикл итог <текст>."
    lines.append("Следующий шаг: " + step)
    if facts["training_sync_status"] != "success":
        lines.append(
            "Сверка тренировок не подтверждена успешной; отсутствие записи не означает пропуск."
        )
    lines.append("Изменение веса не равно измерению потери жира.")
    return "\n".join(lines)
