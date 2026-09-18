"""Explicit shared weekly plans, observed progress and one main-bot review."""

import re
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Notice, Record, Store
from health_agent.pilot.food_focus import FoodFocus
from health_agent.pilot.food_history import build_food_history
from health_agent.pilot.sleep_context import night_contexts
from health_agent.pilot.weekly_evidence import evidence, report_text

ZONE = ZoneInfo("Europe/Moscow")
SETTING = "coaching-cycle"
BUTTON = re.compile(r"^(Принять|Упростить) неделю (\d{2}\.\d{2}\.\d{4})$")
DRAFT = re.compile(r"Черновик общей недели (\d{4}-\d{2}-\d{2})")


def settings(store: Store, profile: UUID) -> dict[str, Any]:
    row = store.by_source(profile, "shared", "settings", SETTING)
    return row.payload if row else {}


def enabled(store: Store, profile: UUID) -> bool:
    return settings(store, profile).get("enabled") is True


def owns_weeklies(store: Store, profile: UUID) -> bool:
    config = settings(store, profile)
    return bool(config and config.get("replace_domain_weeklies", True))


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _next_monday(day: date) -> date:
    return _monday(day) + timedelta(days=7)


def keyboard(text: str) -> dict[str, Any] | None:
    match = DRAFT.search(text)
    if match:
        day = date.fromisoformat(match[1]).strftime("%d.%m.%Y")
        return {
            "keyboard": [[f"Принять неделю {day}"], [f"Упростить неделю {day}"]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    if text.startswith(("Общая неделя принята", "Общий цикл выключен")):
        return {"remove_keyboard": True}
    if text.startswith("Сверка общей недели"):
        return {
            "keyboard": [["Неделя: получается"], ["Неделя: нужна помощь"]],
            "resize_keyboard": True,
            "one_time_keyboard": True,
        }
    return None


def context(store: Store, profile: UUID, now: datetime) -> dict[str, Any]:
    if not enabled(store, profile):
        return {}
    today = now.astimezone(ZONE).date().isoformat()
    plans = [
        r.payload
        for r in store.list(profile, "shared", "cycle_plan", limit=30)
        if r.payload.get("status") == "accepted" and r.payload["through_date"] >= today
    ]
    return {
        "plans": plans[:2],
        "proposals_are_not_accepted": True,
        "commands": "/цикл; /цикл итог <текст>; /тренировка YYYY-MM-DD <что сделал>; /вес <кг>",
    }


class WeeklyCycle:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _record(self, profile: UUID, kind: str, first: date) -> Record | None:
        return self.store.by_source(profile, "shared", kind, first.isoformat())

    def proposal(self, profile: UUID, first: date, now: datetime) -> Record:
        existing = self._record(profile, "cycle_proposal", first)
        if existing:
            return existing
        last = first + timedelta(days=6)
        goals = [
            r.payload
            for r in self.store.list(profile, "shared", "goal", limit=100)
            if r.payload.get("status", "active") in {"active", "planned"}
            and r.payload.get("priority") == "primary"
            and r.payload.get("period_start", "0000") <= last.isoformat()
            and r.payload.get("period_end", "9999") >= first.isoformat()
        ]
        trips = night_contexts(self.store, profile, first, last + timedelta(days=1))
        away = [n for n in trips if n["location"] == "away"]
        history = build_food_history(self.store, profile, now, days=7)
        focus = (
            "В поездке заранее выбирать доступный полдник по своему плану."
            if away
            else FoodFocus._suggest(history)
        )
        target = settings(self.store, profile).get("training_sessions", 2)
        if (
            not isinstance(target, int)
            or isinstance(target, bool)
            or not 1 <= target <= 7
        ):
            target = 2
        previous = self._record(profile, "cycle_plan", first - timedelta(days=7))
        feedback = self._result(profile, previous) if previous else None
        if feedback and re.search(
            r"не успева|не успел|нет времени|слишком много", feedback, re.IGNORECASE
        ):
            target = 1
            focus = "Для одного занятого дня заранее выбрать удобный полдник по своему плану."
        return self.store.put(
            profile,
            "shared",
            "cycle_proposal",
            first.isoformat(),
            {
                "from_date": first.isoformat(),
                "through_date": last.isoformat(),
                "goals": goals,
                "away_nights": away,
                "food_focus": focus,
                "training_sessions": target,
                "previous_feedback": feedback,
                "status": "proposed",
                "revision": 1,
            },
            at=now,
        )

    @staticmethod
    def render(plan: Record, *, accepted: bool = False) -> str:
        p = plan.payload
        first, last = (
            date.fromisoformat(p["from_date"]),
            date.fromisoformat(p["through_date"]),
        )
        lines = [
            f"Общая неделя {first:%d.%m}–{last:%d.%m} принята."
            if accepted
            else f"Черновик общей недели {first.isoformat()} · {first:%d.%m}–{last:%d.%m}."
        ]
        lines.extend("🎯 " + g["title"] for g in p.get("goals", [])[:2])
        lines.append("🍽 Одна задача: " + p["food_focus"])
        lines.append(
            f"🏃 Привычные короткие тренировки: {p['training_sessions']} за неделю. Дни выбирай по доступности."
        )
        if p.get("away_nights"):
            lines.append(
                "🧳 Учтены ночи вне дома. Для занятий выбирай доступный привычный вариант; воздух спальни к этим ночам не относим."
            )
        if p.get("previous_feedback"):
            lines.append("Из прошлого итога: " + str(p["previous_feedback"])[:240])
        lines.append(
            "Калории считаем без лимита. Самочувствие — из утреннего опроса; свежий вес можно записать /вес <кг>."
        )
        if not accepted:
            lines.append("Прими кнопкой или упрости черновик. Пока это предложение.")
        return "\n".join(lines)

    def _reply(self, profile: UUID, key: str, text: str, now: datetime) -> str:
        row = self.store.put(
            profile, "shared", "cycle_reply", key, {"text": text}, at=now
        )
        return str(row.payload["text"])

    def handle(
        self, profile: UUID, text: str, key: str, now: datetime, domain: str
    ) -> str | None:
        text = text.strip()
        button = BUTTON.fullmatch(text)
        if not (
            text.casefold().startswith(("/цикл", "/вес ", "/тренировка ", "неделя:"))
            or button
            or domain == "sleep"
            and text.casefold() == "/неделя"
            or domain == "training"
            and text.casefold() in {"/план", "/сохранить план"}
        ):
            return None
        cached = self.store.by_source(profile, "shared", "cycle_reply", key)
        if cached:
            return str(cached.payload["text"])
        if text.casefold() == "/цикл вкл":
            setting = self.store.by_source(profile, "shared", "settings", SETTING)
            if setting:
                self.store.patch(
                    profile, setting.id, {**setting.payload, "enabled": True}
                )
                return self._reply(
                    profile, key, "Общий цикл включён. Текущий план: /цикл.", now
                )
        if not enabled(self.store, profile):
            return None
        today = now.astimezone(ZONE).date()
        first = _next_monday(today)
        argument = (
            text[len("/цикл") :].strip() if text.casefold().startswith("/цикл") else ""
        )
        if button:
            try:
                day_number, month_number, year_number = map(int, button[2].split("."))
                first = date(year_number, month_number, day_number)
            except ValueError:
                return self._reply(
                    profile,
                    key,
                    "Не удалось разобрать дату недели. Посмотреть предложение: /цикл.",
                    now,
                )
            argument = {"Принять": "принять", "Упростить": "упростить"}[button[1]]
        if text.casefold() == "/сохранить план":
            argument = "принять"
        active = self._record(profile, "cycle_plan", _monday(today))
        if text.casefold().startswith("/вес "):
            try:
                value = float(text.split(maxsplit=1)[1].replace(",", "."))
                if not 0 < value < 500:
                    raise ValueError
            except ValueError:
                return self._reply(
                    profile, key, "Напиши текущий вес числом: /вес <кг>.", now
                )
            self.store.put(
                profile,
                "shared",
                "weight",
                key,
                {"weight_kg": value, "source": "user_report"},
                at=now,
            )
            return self._reply(
                profile,
                key,
                f"Записал твой текущий вес: {value:g} кг. Учту в общем итоге; это не измерение жировой массы.",
                now,
            )
        if text.casefold().startswith("/тренировка "):
            parts = text.split(maxsplit=2)
            try:
                day = date.fromisoformat(parts[1])
                if len(parts) != 3 or not today - timedelta(days=14) <= day <= today:
                    raise ValueError
            except (ValueError, IndexError):
                return self._reply(
                    profile,
                    key,
                    "Записать выполненное: /тренировка YYYY-MM-DD <что сделал>. Дата должна быть в последних 14 днях.",
                    now,
                )
            self.store.put(
                profile,
                "training",
                "manual_activity",
                key,
                {
                    "date": day.isoformat(),
                    "text": parts[2][:500],
                    "source": "user_report",
                    "time_unknown": True,
                },
                at=now,
            )
            return self._reply(
                profile,
                key,
                "Сохранил выполненную тренировку по твоему сообщению. С записью COROS за тот же день повторно не суммирую.",
                now,
            )
        if text.casefold().startswith("неделя:"):
            argument = "итог " + text.split(":", 1)[1].strip()
        if argument.startswith("итог"):
            plan = active or self._record(
                profile, "cycle_plan", _monday(today) - timedelta(days=7)
            )
            if not plan or plan.payload.get("status") != "accepted":
                return self._reply(
                    profile,
                    key,
                    "Принятой текущей или прошедшей недели пока нет. Посмотреть предложение: /цикл.",
                    now,
                )
            result = argument[len("итог") :].strip()
            if result:
                self.store.put(
                    profile,
                    "shared",
                    "cycle_result",
                    key,
                    {"plan_id": plan.id, "text": result[:500]},
                    at=now,
                )
            reply = self.review(profile, plan, now)
            if result in {"нужна помощь", "получается"}:
                reply += "\nНапиши /цикл итог и одну конкретную вещь, которая получилась или мешает — так смогу учесть её в следующем плане."
            return self._reply(profile, key, reply, now)
        if argument == "стоп":
            row = self.store.by_source(profile, "shared", "settings", SETTING)
            assert row
            self.store.patch(profile, row.id, {**row.payload, "enabled": False})
            return self._reply(
                profile,
                key,
                "Общий цикл выключен. История сохранена; обычные утренние и пищевые напоминания продолжаются.",
                now,
            )
        if argument in {"принять", "упростить"}:
            proposal = self._record(profile, "cycle_proposal", first)
            if not proposal or first < _monday(today) or first > _next_monday(today):
                return self._reply(
                    profile,
                    key,
                    "Это предложение уже недоступно. Свежий план: /цикл.",
                    now,
                )
            accepted = self._record(profile, "cycle_plan", first)
            if accepted:
                self._mirror(profile, accepted, now)
                return self._reply(
                    profile, key, self.render(accepted, accepted=True), now
                )
            if argument == "упростить":
                proposal = self.store.patch(
                    profile,
                    proposal.id,
                    {
                        **proposal.payload,
                        "training_sessions": 1,
                        "revision": proposal.payload["revision"] + 1,
                        "food_focus": "Для одного занятого дня заранее выбрать удобный полдник по своему плану.",
                    },
                )
                return self._reply(profile, key, self.render(proposal), now)
            plan = self.store.put(
                profile,
                "shared",
                "cycle_plan",
                first.isoformat(),
                {
                    **proposal.payload,
                    "status": "accepted",
                    "accepted_at": now.isoformat(),
                    "accepted_source_key": key,
                    "proposal_id": proposal.id,
                },
                at=now,
            )
            self._mirror(profile, plan, now)
            return self._reply(
                profile,
                key,
                "Общая неделя принята.\n"
                + self.render(plan, accepted=True)
                + "\nВ среду сверим продвижение, в воскресенье — общий итог.",
                now,
            )
        if argument:
            return self._reply(
                profile,
                key,
                "Общий план: /цикл. Итог или трудность: /цикл итог <текст>. Выключить: /цикл стоп.",
                now,
            )
        plan = (
            active
            if active and active.payload.get("status") == "accepted"
            else self._record(profile, "cycle_plan", first)
        )
        reply = (
            self.render(plan, accepted=True)
            if plan
            else self.render(self.proposal(profile, first, now))
        )
        return self._reply(profile, key, reply, now)

    def _mirror(self, profile: UUID, plan: Record, now: datetime) -> None:
        p = plan.payload
        focus = self.store.put(
            profile,
            "food",
            "focus",
            "cycle:" + plan.id,
            {
                "text": p["food_focus"],
                "from_date": p["from_date"],
                "through_date": p["through_date"],
                "status": "active",
                "source": "user_accepted_cycle",
                "cycle_plan_id": plan.id,
            },
            at=now,
        )
        FoodFocus(self.store)._point(profile, now, focus_id=focus.id)
        self.store.put(
            profile,
            "training",
            "accepted_plan",
            "cycle:" + plan.id,
            {
                "text": self.render(plan, accepted=True),
                "cycle_plan_id": plan.id,
                "from_date": p["from_date"],
                "through_date": p["through_date"],
                "training_sessions": p["training_sessions"],
                "source": "user_accepted_cycle",
            },
            at=now,
        )

    def _result(self, profile: UUID, plan: Record) -> str | None:
        rows = [
            r
            for r in self.store.list(profile, "shared", "cycle_result", limit=100)
            if r.payload.get("plan_id") == plan.id
        ]
        return str(rows[0].payload["text"]) if rows else None

    def review(self, profile: UUID, plan: Record, now: datetime) -> str:
        p = plan.payload
        facts = evidence(
            self.store,
            profile,
            date.fromisoformat(p["from_date"]),
            date.fromisoformat(p["through_date"]),
            now,
        )
        return report_text(facts, p, self._result(profile, plan))

    def due(self, profile: UUID, now: datetime) -> list[Notice]:
        if not enabled(self.store, profile):
            return []
        local = now.astimezone(ZONE)
        if not 8 <= local.hour < 21:
            return []
        today = local.date()
        receipts = self.store.list(profile, "sleep", "notice", limit=1000)
        delivered = {r.source_key for r in receipts}
        sunday = today - timedelta(days=(today.weekday() + 1) % 7)
        anchor = datetime.combine(sunday, time(18), ZONE)
        if local < anchor:
            anchor -= timedelta(days=7)
        key = "cycle:weekly:" + anchor.date().isoformat()
        config = self.store.by_source(profile, "shared", "settings", SETTING)
        assert config
        if (
            timedelta(0) <= local - anchor <= timedelta(days=2)
            and config.at <= anchor
            and key not in delivered
        ):
            first = anchor.date() - timedelta(days=6)
            plan = self._record(profile, "cycle_plan", first)
            prefix = self.review(profile, plan, now) + "\n\n" if plan else ""
            next_plan = self.proposal(profile, first + timedelta(days=7), now)
            accepted = self._record(profile, "cycle_plan", first + timedelta(days=7))
            return [
                Notice(
                    key,
                    prefix
                    + self.render(accepted or next_plan, accepted=accepted is not None),
                )
            ]
        first = _monday(today)
        plan = self._record(profile, "cycle_plan", first)
        check_at = datetime.combine(first + timedelta(days=2), time(18), ZONE)
        key = "cycle:checkin:" + first.isoformat()
        if (
            plan
            and plan.payload.get("status") == "accepted"
            and timedelta(0) <= local - check_at <= timedelta(days=1)
            and key not in delivered
        ):
            return [
                Notice(
                    key,
                    "Сверка общей недели\n"
                    + self.review(profile, plan, now)
                    + "\nКак получается следовать плану?",
                )
            ]
        if not any(k.startswith("cycle:") for k in delivered) and not self.store.list(
            profile, "shared", "cycle_plan", limit=1
        ):
            return [
                Notice(
                    "cycle:welcome",
                    self.render(self.proposal(profile, _next_monday(today), now)),
                )
            ]
        return []
