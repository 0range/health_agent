"""A short weekly observation and one explicit, goal-grounded suggestion."""

import re
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from health_agent.pilot.contracts import Store
from health_agent.pilot.health_insights import ZONE, timestamp
from health_agent.pilot.health_insights_report import HealthInsights
from health_agent.pilot.weekly_goals import (
    current_goals,
    goal_label,
    training_label,
    training_targets,
)


def training_mix(store: Store, profile: UUID, first: date, last: date, now: datetime) -> dict[str, Any]:
    days: dict[str, set[str]] = {"strength": set(), "padel": set(), "other": set()}
    seen: set[str] = set()
    unknown_manual = 0
    truncated = False
    for kind in ("activity", "manual_activity"):
        rows = store.list(profile, "training", kind, limit=1000)
        truncated = truncated or len(rows) == 1000
        for row in rows:
            if row.at > now:
                continue
            p = row.payload
            try:
                day = date.fromisoformat(p["date"]) if kind == "manual_activity" else row.at.astimezone(ZONE).date()
            except (KeyError, TypeError, ValueError):
                continue
            if not first <= day <= last:
                continue
            if kind == "activity":
                identifier = str(p.get("id") or row.source_key)
                if identifier in seen:
                    continue
                seen.add(identifier)
                text = str(p.get("sport") or "").casefold()
                field = ("strength" if re.search(r"\b(?:strength|weight training|силов\w*)\b", text)
                         else "padel" if text.strip() in {"padel", "падел", "падл"} else "other")
                days[field].add(day.isoformat())
            else:
                text = str(p.get("text") or "").casefold()
                if re.search(r"\b(?:не|нет|без|вместо|план\w*|завтра|хочу|отмен\w*|пропуст\w*)\b", text):
                    unknown_manual += 1
                    continue
                matched = False
                for field, pattern in (("strength", r"\bсилов\w*\b"), ("padel", r"\b(?:падел\w*|падл\w*)\b")):
                    if re.search(pattern, text):
                        days[field].add(day.isoformat())
                        matched = True
                if not matched:
                    unknown_manual += 1
    return {**{k + "_days": len(v) for k, v in days.items()}, "unknown_manual": unknown_manual,
            "count_unit": "distinct_days_by_type", "truncated": truncated}


def _choose_action(facts: dict, mix: dict, targets: dict, feedback: str | None) -> dict[str, str]:
    if targets and any(mix[k + "_days"] < n for k, n in targets.items()):
        if feedback and re.search(r"не успева|не успел|нет времени|слишком много", feedback, re.IGNORECASE):
            return {"kind": "training_simplify", "text": "Выбери время для одной привычной короткой силовой.",
                    "reason": "В прошлом итоге ты написал, что не хватило времени; общий ориентир сохраняем."}
        return {"kind": "training_rhythm", "text": f"Заранее выбери дни для ритма «{training_label(targets)}».",
                "reason": "Такой набор занятий пока не подтверждён записями."}
    latest = facts["weight"].get("latest")
    now = datetime.fromisoformat(facts["as_of"]).astimezone(ZONE)
    if latest is None or (now.date() - date.fromisoformat(latest["date"])).days > 7:
        return {"kind": "weigh", "text": "Сделай замеры в четверг и воскресенье утром: /замер.",
                "reason": "Без свежего веса движение к цели пока не оценить."}
    if facts["food"]["four_slot_days"] < 3:
        return {"kind": "food_coverage", "text": "Три дня запиши все приёмы еды вместе с добавками и перекусами.",
                "reason": "Сейчас записи слишком неполные для оценки питания."}
    dinner: dict[str, Any] = next((a for a in facts["associations"] if a["question"] == "dinner_gap"), {})
    if dinner.get("status") == "exploratory" and dinner.get("difference_hours", 0) <= -0.5:
        return {"kind": "dinner_trial", "text": "Попробуй неделю ужинать на час раньше обычного.",
                "reason": "После более поздних ужинов сон был короче; это гипотеза для проверки, не доказанная причина."}
    delta = facts["weight"]["delta_kg"]
    if delta is not None and delta >= 0:
        return {"kind": "food_portions", "text": "В три дня уточни вес порций и добавки к обычной еде.",
                "reason": "Средний вес пока не снижается; сначала уточним оценку питания."}
    return {"kind": "keep_routine", "text": "Сохрани текущий режим питания и тренировок ещё на неделю.",
            "reason": "Оснований для нового ограничения сейчас нет; проверим следующую динамику."}


def render(payload: dict[str, Any]) -> str:
    f, mix = payload["evidence"], payload["training_mix"]
    first, last = date.fromisoformat(f["recent_from"]), date.fromisoformat(f["through_date"])
    suffix = f" · на {datetime.fromisoformat(f['as_of']).astimezone(ZONE):%H:%M}" if f["partial_day"] else ""
    lines = [f"Неделя {first:%d.%m}–{last:%d.%m}{suffix}", "🎯 " + goal_label(payload["next_goals"])]
    weight = f["weight"]
    if weight["delta_kg"] is not None:
        lines.append(f"⚖️ Средний вес {weight['delta_kg']:+g} кг к прошлой неделе ({weight['recent_days']}/{weight['previous_days']} дней замеров). Потерю жира это не подтверждает.")
    else:
        latest = weight.get("latest")
        note = f"Последний вес {latest['kg']:g} кг ({date.fromisoformat(latest['date']):%d.%m}); " if latest else ""
        lines.append("⚖️ " + note + "динамику жира пока не оценить.")
    fat = f.get("body_composition", {}).get("body_fat_percent", {})
    if fat.get("delta") is not None:
        lines.append(f"📏 Доля жира по весам: {fat['recent']:g}%, {fat['delta']:+g} п.п. к прошлой неделе — оценка прибора.")
    kcal = f["food"]["calories"]
    if kcal["recent"] is not None:
        lines.append(f"🍽 ≈{kcal['recent']:.0f} ккал/день по {kcal['recent_days']} дням с четырьмя записанными приёмами. Это оценка, не доказанный дефицит.")
    else:
        lines.append(f"🍽 Еда записана в {f['food']['days']}/7 дней; полной оценки калорий пока нет.")
    lines.append(f"🏋️ По записям: силовые — {mix['strength_days']} дн., падел — {mix['padel_days']} дн.; другие занятия — {mix['other_days']} дн. Отсутствие записи не означает пропуск.")
    sleep, rested = f["recovery"]["sleep_hours"], f["recovery"]["rested"]
    if sleep["recent"] is not None:
        minutes = round(sleep["recent"] * 60)
        line = f"😴 Сон {minutes // 60} ч {minutes % 60:02d} мин ({sleep['recent_days']} ночей)"
        if sleep["delta"] is not None:
            line += f", {round(sleep['delta'] * 60):+d} мин к прошлой неделе"
        if rested["recent"] is not None:
            line += f"; выспался {rested['recent']:g}/10"
        lines.append(line + ".")
    if payload["data_gaps"]:
        lines.append("📡 Неполные данные: " + ", ".join(payload["data_gaps"]) + ".")
    action = payload["action"]
    lines.append(f"➡️ Следующая неделя: {action['text']} {action['reason']}")
    return "\n".join(lines)


class WeeklyBrief:
    def __init__(self, store: Store, insights: HealthInsights) -> None:
        self.store, self.insights = store, insights

    def report(self, profile: UUID, now: datetime, sunday: date, key: str) -> str:
        saved = self.store.by_source(profile, "shared", "weekly_brief", key)
        if saved is not None:
            return str(saved.payload["text"])
        first = sunday - timedelta(days=6)
        facts = self.insights.build(profile, now, through_date=sunday)
        goals = current_goals(self.store, profile, first, sunday)
        next_goals = current_goals(self.store, profile, sunday+timedelta(days=1), sunday+timedelta(days=7))
        mix = training_mix(self.store, profile, first, sunday, now)
        gaps = []
        for name, source, max_age in (("WHOOP", facts["whoop"], timedelta(hours=12)),
                                      ("COROS", {"status": facts["training"]["sync_status"],
                                                 "sync_at": facts["training"]["sync_at"]}, timedelta(hours=36))):
            synced = timestamp(source.get("sync_at"))
            if source.get("status") not in {"ok", "success"} or synced is None or not timedelta(0) <= now-synced <= max_age:
                gaps.append(name)
        if facts["truncated"] or mix["truncated"]:
            gaps.append("часть истории за пределами выборки")
        plan = self.store.by_source(profile, "shared", "cycle_plan", first.isoformat())
        feedback = None
        if plan:
            feedback = next((str(r.payload["text"]) for r in self.store.list(profile, "shared", "cycle_result", limit=100)
                             if r.payload.get("plan_id") == plan.id and r.at <= now), None)
        targets = training_targets(next_goals)
        payload = {"version": 1, "evidence": facts, "current_goals": goals, "next_goals": next_goals,
                   "current_targets": training_targets(goals), "next_targets": targets,
                   "training_mix": mix, "data_gaps": gaps, "feedback": feedback,
                   "action": _choose_action(facts, mix, targets, feedback), "suggestion_not_accepted_plan": True}
        text = render(payload)
        row = self.store.put(profile, "shared", "weekly_brief", key, {**payload, "text": text}, at=now)
        return str(row.payload["text"])
