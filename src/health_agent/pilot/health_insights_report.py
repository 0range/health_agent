"""Short, reproducible observations; wording never upgrades association to cause."""

from collections.abc import Callable
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from health_agent.pilot.contracts import Store
from health_agent.pilot.health_insights import build_insights, period, timestamp

WhoopSource = Callable[[UUID, date, date, datetime], dict[str, Any]]


def render(report: dict[str, Any]) -> str:
    food, training, weight = report["food"], report["training"], report["weight"]
    first = date.fromisoformat(report["recent_from"]).strftime("%d.%m")
    last = date.fromisoformat(report["through_date"]).strftime("%d.%m")
    lines = [f"🧩 Общий разбор {first}–{last} · завершённые дни"]
    if weight["delta_kg"] is not None:
        lines.append(f"⚖️ Средний вес: {weight['recent']:g} кг; {weight['delta_kg']:+g} кг к прошлой неделе ({weight['recent_days']} и {weight['previous_days']} дней измерений). Предварительное сравнение: это изменение веса, не измеренная потеря жира.")
    else:
        latest = weight.get("latest")
        detail = f" Последний датированный: {latest['kg']:g} кг, {date.fromisoformat(latest['date']):%d.%m}." if latest else ""
        lines.append("⚖️ Динамику веса пока не оценить: нужно хотя бы по 2 дня измерений в двух неделях." + detail)
    body = report.get("body_composition", {}).get("latest")
    if body:
        parts = [f"{label} {body[key]:g}{unit}" for key, label, unit in (
            ("body_fat_percent", "жир", "%"), ("muscle_mass_kg", "мышцы", " кг"),
            ("muscle_percent", "мышцы", "%")) if body.get(key) is not None]
        lines.append(f"📏 Весы {date.fromisoformat(body['date']):%d.%m}: {', '.join(parts)}. Это оценки весов.")
    lines.append(f"🍽 Еда записана в {food['days']}/7 дней; все четыре приёма — в {food['four_slot_days']}/7. Это полнота записей, не оценка того, сколько ты ел.")
    calories, protein = food["calories"], food["protein"]
    if calories["recent"] is not None:
        note = f" По дням с четырьмя приёмами: ≈{calories['recent']:.0f} ккал/день ({calories['recent_days']} дн.)."
        if protein["recent"] is not None:
            note += f" Белок ≈{protein['recent']:.0f} г/день ({protein['recent_days']} дн.)."
        lines.append(note.strip() + " Калории оценочные; дефицит этим не доказан.")
    if food["unknown_calories"]:
        lines.append(f"Записей без оценки калорий: {food['unknown_calories']}.")
    training_note = " + есть занятия без длительности" if training["duration_missing"] else ""
    lines.append(f"🏃 COROS: записей — {training['sessions']}, дней с занятиями — {training['days']}, всего {training['minutes']} мин{training_note}; неделей раньше — {training['previous_days']} дн. и {training['previous_minutes']} мин.")
    if training["manual_days_without_coros"]:
        lines.append(f"Отдельно вручную отмечены занятия в {training['manual_days_without_coros']} дн. без записи COROS; их длительность не прибавляю.")
    recovery = report["recovery"]
    labels = {"sleep_hours": ("сон", "ч"), "hrv_ms": ("HRV", "мс"), "resting_hr": ("пульс покоя", "уд/мин")}
    for key, (label, unit) in labels.items():
        value = recovery[key]
        if value["recent"] is None:
            continue
        text = f"{label} {value['recent']:g} {unit} ({value['recent_days']}/7 ночей)"
        if key == "sleep_hours":
            minutes = round(value['recent'] * 60)
            text = f"сон в среднем {minutes // 60} ч {minutes % 60:02d} мин ({value['recent_days']}/7 ночей)"
        if value["enough_for_comparison"]:
            if key == "sleep_hours":
                delta = round(value["delta"] * 60)
                text += f", на {abs(delta)} мин {'больше' if delta >= 0 else 'меньше'}, чем неделей раньше ({value['previous_days']}/7 ночей)"
            else:
                text += f", раньше {value['previous']:g} ({value['previous_days']}/7)"
        lines.append("😴 " + text + ".")
    if recovery["sleep_hours"]["recent"] is None:
        lines.append("😴 За эту неделю нет пригодных основных ночей WHOOP.")
    if recovery["rested"]["recent"] is not None:
        rested = recovery["rested"]
        lines.append(f"🙂 По твоим ответам: выспался в среднем на {rested['recent']:g}/10 ({rested['recent_days']} дн.).")
    associations = [a for a in report["associations"] if a["status"] == "exploratory"]
    for a in associations[:1]:
        label = ("после ужина менее чем за 2 ч до сна против более раннего ужина"
                 if a["question"] == "dinner_gap" else "после записанной тренировки против ночей без записи COROS")
        minutes = round(a["difference_hours"] * 60)
        difference = f"средний сон отличался на {minutes:+d} мин"
        if abs(minutes) < 15:
            difference = f"средняя длительность сна почти одинаковая (разница {minutes:+d} мин)"
        lines.append(f"🔎 Наблюдение за 28 дней: {label} {difference}; {a['left_nights']} против {a['right_nights']} ночей. Совпадение не доказывает влияние; другие причины не исключены.")
    if not associations:
        counts = "; ".join(f"{'ужин' if a['question']=='dinner_gap' else 'тренировки'}: {a['left_nights']}/{a['right_nights']} ночей в группах" for a in report["associations"])
        lines.append(f"🔎 Что влияет на сон: пока мало сопоставимых ночей ({counts}). Нельзя честно назвать причину или сказать «это работает».")
    now = datetime.fromisoformat(report["as_of"])
    whoop_sync = timestamp(report["whoop"].get("sync_at"))
    coros_sync = timestamp(training.get("sync_at"))
    whoop_stale = whoop_sync is None or not timedelta(0) <= now - whoop_sync <= timedelta(hours=12)
    coros_stale = coros_sync is None or not timedelta(0) <= now - coros_sync <= timedelta(hours=36)
    if whoop_stale or report["whoop"].get("status") != "ok":
        lines.append("📡 WHOOP: свежая успешная синхронизация не подтверждена; это пробел данных.")
    if coros_stale or training["sync_status"] != "success":
        lines.append("📡 COROS: свежая успешная синхронизация не подтверждена; отсутствие занятий не считаю пропусками.")
    if report["truncated"]:
        lines.append("👀 Выборка ограничена; итог охватывает не все доступные записи.")
    latest = weight.get("latest")
    if whoop_stale or coros_stale:
        action = "восстановить свежую синхронизацию перед выводами по нагрузке и восстановлению."
    elif latest is None or (now.date() - date.fromisoformat(latest["date"])).days > 7:
        action = "добавить свежий замер: /замер или /вес <кг>. Датированный ряд нужен, чтобы проверить движение к цели."
    elif food["four_slot_days"] < 3:
        action = "три дня подряд записать все четыре приёма и дополнения — тогда можно сопоставлять питание с весом."
    elif weight["delta_kg"] is None:
        action = "продолжить замеры дважды в неделю; первое предварительное сравнение появится при 2 днях в каждой из двух недель."
    else:
        action = "на следующую неделю выбрать одно изменение и записывать его; по текущим совпадениям причины не установлены."
    lines.append("🎯 Один следующий шаг: " + action)
    return "\n".join(lines)


class HealthInsights:
    def __init__(self, store: Store, whoop_source: WhoopSource | None = None) -> None:
        self.store, self.whoop_source = store, whoop_source

    def build(self, profile: UUID, now: datetime, *, through_date: date | None = None) -> dict[str, Any]:
        first, last = period(now, through_date)
        whoop: dict[str, Any] = {"status": "not_connected", "records": []}
        if self.whoop_source is not None:
            try:
                whoop = self.whoop_source(profile, first, last, now)
            except Exception:  # noqa: BLE001 -- report missing source without exposing DB details
                whoop = {"status": "unavailable", "records": []}
        return build_insights(self.store, profile, now, whoop, through_date=through_date)

    def report(self, profile: UUID, now: datetime, key: str) -> str:
        saved = self.store.by_source(profile, "shared", "health_insight", key)
        if saved is not None:
            return str(saved.payload["text"])
        evidence = self.build(profile, now)
        text = render(evidence)
        row = self.store.put(profile, "shared", "health_insight", key,
                             {"text": text, "evidence": evidence, "version": 1}, at=now)
        return str(row.payload["text"])
