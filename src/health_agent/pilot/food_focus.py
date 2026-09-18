"""One explicitly chosen food focus, separate from meal facts and reminders."""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Record, Store

_ZONE = ZoneInfo("Europe/Moscow")


class FoodFocus:
    def __init__(self, store: Store) -> None:
        self.store = store

    def _state(self, profile: UUID) -> Record | None:
        return self.store.by_source(profile, "food", "focus_state", "weekly-focus")

    def _point(self, profile: UUID, now: datetime, **values: Any) -> None:
        state = self._state(profile)
        payload = {**(state.payload if state else {}), **values}
        if state:
            self.store.patch(profile, state.id, payload)
        else:
            self.store.put(profile, "food", "focus_state", "weekly-focus", payload, at=now)

    def _selected(self, profile: UUID, key: str) -> Record | None:
        state = self._state(profile)
        return self.store.get(profile, str(state.payload[key])) if state and state.payload.get(key) else None

    def current(self, profile: UUID, now: datetime) -> Record | None:
        focus = self._selected(profile, "focus_id")
        today = now.astimezone(_ZONE).date().isoformat()
        if (focus and focus.payload["status"] == "active"
                and focus.payload["from_date"] <= today <= focus.payload["through_date"]):
            return focus
        # A future accepted shared week must not hide the current food task.
        return next((r for r in self.store.list(profile, 'food', 'focus', limit=100)
                     if r.payload.get('status') == 'active'
                     and r.payload['from_date'] <= today <= r.payload['through_date']), None)

    def recent(self, profile: UUID, now: datetime) -> Record | None:
        focus = self._selected(profile, "focus_id")
        today = now.astimezone(_ZONE).date()
        if (focus and focus.payload["from_date"] <= today.isoformat()
                and date.fromisoformat(focus.payload["through_date"]) >= today - timedelta(days=7)):
            return focus
        return None

    def plan_line(self, profile: UUID, now: datetime) -> str:
        focus = self.current(profile, now)
        if not focus:
            return "🎯 Фокус недели ещё не выбран. Выбрать: /фокус."
        return f"🎯 Согласованный фокус до {date.fromisoformat(focus.payload['through_date']):%d.%m}: {focus.payload['text']}"

    def handle(
        self, profile: UUID, text: str, source_key: str, now: datetime,
        history: dict[str, Any],
    ) -> str | None:
        if not text.strip() or text.split(maxsplit=1)[0].lower() != "/фокус":
            return None
        previous = self.store.by_source(profile, "food", "focus_reply", source_key)
        if previous:
            return str(previous.payload["text"])
        value = text[len("/фокус"):].strip()
        answer = self._handle(profile, value, source_key, now, history)
        record = self.store.put(profile, "food", "focus_reply", source_key, {"text": answer}, at=now)
        return str(record.payload["text"])

    def _handle(self, profile: UUID, value: str, key: str, now: datetime, history: dict[str, Any]) -> str:
        today = now.astimezone(_ZONE).date()
        lowered = value.casefold()
        current = self.current(profile, now)
        if not value:
            if current:
                return self.plan_line(profile, now) + "\nРезультат или трудность: /фокус итог <текст>. Изменить: /фокус <новая задача>. Завершить: /фокус стоп."
            proposal = self._selected(profile, "proposal_id")
            if (not proposal or proposal.payload.get("accepted")
                    or proposal.payload["through_date"] < today.isoformat()):
                proposed = self._suggest(history)
                proposal = self.store.put(profile, "food", "focus_proposal", key, {
                    "text": proposed, "through_date": (today + timedelta(days=6)).isoformat(),
                }, at=now)
                self._point(profile, now, proposal_id=proposal.id)
            return (f"🎯 Предлагаю фокус на 7 дней: {proposal.payload['text']}\n"
                    "Принять: /фокус принять. Или напиши /фокус и свою задачу. Пока фокус не принят.")
        if lowered == "стоп":
            if current:
                self.store.patch(profile, current.id, {**current.payload, "status": "stopped", "closed_at": now.isoformat()})
            return "Фокус завершён. История сохранена; новый можно выбрать командой /фокус."
        if lowered.startswith("итог") and (lowered == "итог" or lowered[4].isspace()):
            result = value[4:].strip()
            focus = current or self.recent(profile, now)
            if not focus:
                return "Сначала выбери фокус: /фокус."
            if not result or len(result) > 400:
                return "Напиши коротко, что получилось или мешало: /фокус итог <текст> (до 400 символов)."
            self.store.put(profile, "food", "focus_result", key, {"focus_id": focus.id, "text": result}, at=now)
            return "Сохранил твой итог по фокусу. Учту его в /неделя."
        proposal = None
        if lowered == "принять":
            if current:
                return self.plan_line(profile, now)
            proposal = self._selected(profile, "proposal_id")
            if (not proposal or proposal.payload.get("accepted")
                    or proposal.payload["through_date"] < today.isoformat()):
                return "Нет свежего предложения. Выбрать новое: /фокус."
            value = str(proposal.payload["text"])
        if not 3 <= len(value) <= 240:
            return "Опиши одну выполнимую задачу: /фокус <текст от 3 до 240 символов>."
        focus = self.store.put(profile, "food", "focus", key, {
            "text": value, "from_date": today.isoformat(),
            "through_date": (today + timedelta(days=6)).isoformat(),
            "status": "active", "source": "user_accepted" if proposal else "user",
            "proposal_id": proposal.id if proposal else None,
        }, at=now)
        if current and current.id != focus.id:
            self.store.patch(profile, current.id, {**current.payload, "status": "replaced", "closed_at": now.isoformat()})
        self._point(profile, now, focus_id=focus.id)
        if proposal:
            self.store.patch(profile, proposal.id, {**proposal.payload, "accepted": focus.id})
        return self.plan_line(profile, now) + "\nБуду учитывать в плане и недельном итоге. Калорийный лимит не добавляю."

    @staticmethod
    def _suggest(history: dict[str, Any]) -> str:
        # A suggestion, never an inferred failure or a silently accepted prescription.
        from health_agent.pilot.food_assessment import findings

        missing_fruit = 0
        for meal in history.get("meals", []):
            if meal.get("category") not in {"breakfast", "lunch"} or not meal.get("analysis_available"):
                continue
            _, missing, _ = findings({"foods": meal.get("foods", [])}, meal["category"])
            missing_fruit += any("фрукт" in item for item in missing)
        if missing_fruit >= 2:
            return "Вспоминать про фрукт к завтраку и обеду по выбранному плану."
        return "Заранее выбирать удобный полдник на занятые дни."

    def summary(self, profile: UUID, now: datetime, history: dict[str, Any]) -> str:
        focus = self.recent(profile, now)
        if not focus:
            week = now.astimezone(_ZONE).date().isocalendar()
            return self._handle(profile, "", f"weekly-focus:{week.year}-{week.week}", now, history)
        results = [r for r in self.store.list(profile, "food", "focus_result", limit=100)
                   if r.payload.get("focus_id") == focus.id and r.at <= now]
        result = results[0].payload["text"] if results else None
        lines = [f"🎯 Фокус {date.fromisoformat(focus.payload['from_date']):%d.%m}–{date.fromisoformat(focus.payload['through_date']):%d.%m}: {focus.payload['text']}"]
        if result:
            lines.append(f"По твоему итогу: {result}")
            lines.append("Следующий шаг: решить, что оставить или упростить в этой задаче. Изменить: /фокус <текст>.")
        else:
            lines.append("Результат по фокусу пока неизвестен; по отсутствию записи его не оцениваю.")
            lines.append("Следующий шаг: отметь, что получилось или мешало — /фокус итог <текст>.")
        return "\n".join(lines)
