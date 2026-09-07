"""Durable food capture and meal-interval reminders for the pilot."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from health_agent.pilot.contracts import Attachment, Brain, Notice, Record, Store

_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_NUTRIENTS = (
    "kcal", "protein_g", "fat_g", "carbs_g", "saturated_fat_g", "fiber_g",
    "cholesterol_mg",
)
_QUIET_START = time(22, 0)
_QUIET_END = time(7, 0)


class FoodCoach:
    """Capture meals first, then enrich them and derive durable reminders."""

    def __init__(self, store: Store, brain: Brain) -> None:
        self._store = store
        self._brain = brain

    def handle(
        self, profile_id: UUID, text: str, *, source_key: str, now: datetime,
        attachment: Attachment | None = None,
    ) -> str:
        self._aware(now)
        command = text.strip()
        lowered = command.lower()
        if lowered.startswith("/напоминания выкл"):
            self._setting(profile_id, source_key, False, now)
            return "Напоминания о плане питания выключены."
        if lowered.startswith("/напоминания вкл"):
            self._setting(profile_id, source_key, True, now)
            return "Напоминания о плане питания включены."
        if lowered.startswith("/позже"):
            return self._snooze(profile_id, command, source_key, now)
        if lowered.startswith("/пропустить"):
            return self._control(profile_id, "skip", source_key, now)
        if lowered.startswith("/время"):
            return self._correct(profile_id, command, source_key, now)
        if lowered.startswith("/сегодня"):
            return self._summary(profile_id, now, days=1)
        if lowered.startswith("/неделя"):
            return self._summary(profile_id, now, days=7)
        if not command and attachment is None:
            return "Опишите приём пищи или приложите фотографию."
        return self._meal(profile_id, command, source_key, now, attachment)

    def due(self, profile_id: UUID, now: datetime) -> list[Notice]:
        self._aware(now)
        if not self._reminders_enabled(profile_id):
            return []
        meal = self._latest_meal(profile_id)
        if meal is None or meal.payload.get("category") == "dinner":
            return []
        occurred = self._from_iso(meal.payload["occurred_at"])
        protocol = self._protocol(profile_id)
        hours = self._positive_number(protocol.get("interval_hours")) or 3.5
        target = occurred + timedelta(hours=hours)
        control = self._latest_control(profile_id, meal.id)
        if control is not None:
            action = control.payload.get("action")
            if action == "skip":
                return []
            if action == "snooze":
                target = self._from_iso(control.payload["until"])
        if now < target or self._quiet(now, protocol):
            return []
        key = f"meal:{meal.id}:at:{target.astimezone(UTC).isoformat()}"
        if any(r.source_key == key for r in self._store.list(profile_id, "food", "notice")):
            return []
        return [Notice(key, "Плановый интервал после последнего приёма пищи прошёл. Хотите поесть сейчас?")]

    def _meal(
        self, profile_id: UUID, text: str, source_key: str, now: datetime,
        attachment: Attachment | None,
    ) -> str:
        existing = self._by_source(profile_id, "meal", source_key)
        if existing is not None and existing.payload.get("analysis") is not None:
            return self._feedback(existing.payload)
        if existing is None:
            occurred, cleaned, valid = self._extract_time(text, now)
            if not valid:
                return "Уточните дату или время приёма пищи: указанное время выглядит будущим или слишком давним."
            category = self._category(cleaned, occurred)
            payload: dict[str, Any] = {
                "original": text, "caption": attachment.caption if attachment else "",
                "photo_path": str(attachment.path) if attachment else None,
                "occurred_at": occurred.isoformat(), "captured_at": now.isoformat(),
                "category": category, "category_source": "labelled_heuristic",
                "analysis": None, "analysis_raw": None, "analysis_error": None,
            }
            existing = self._store.put(
                profile_id, "food", "meal", source_key, payload, at=occurred,
            )
        payload = dict(existing.payload)
        try:
            raw = self._brain(
                self._system_prompt(),
                {"meal": {"text": payload["original"], "caption": payload["caption"]},
                 "protocol": self._protocol(profile_id)},
                image_path=Path(payload["photo_path"]) if payload["photo_path"] else None,
            )
            payload["analysis_raw"] = raw
            payload["analysis"] = self._parse_analysis(raw, bool(payload["photo_path"]))
            payload["analysis_error"] = None if payload["analysis"] is not None else "invalid_json"
        except Exception as exc:  # noqa: BLE001 - authorized model callable is a boundary
            payload["analysis_error"] = type(exc).__name__
        updated = self._store.patch(profile_id, existing.id, payload)
        if updated.payload.get("analysis") is None:
            return "Приём пищи сохранён; анализ сейчас недоступен. Напоминание продолжит работать."
        return self._feedback(updated.payload)

    def _correct(self, profile_id: UUID, text: str, source_key: str, now: datetime) -> str:
        meal = self._latest_meal(profile_id)
        if meal is None:
            return "Сначала сохраните приём пищи."
        occurred, _, valid = self._extract_time(text, now)
        if not valid:
            return "Уточните дату или время: оно выглядит будущим или слишком давним."
        # Correction is idempotently recorded as well as applied to the whole payload.
        correction = self._store.put(
            profile_id, "food", "correction", source_key,
            {"meal_id": meal.id, "occurred_at": occurred.isoformat()}, at=now,
        )
        payload = dict(meal.payload)
        payload["occurred_at"] = correction.payload["occurred_at"]
        payload["time_corrected"] = True
        self._store.patch(profile_id, meal.id, payload)
        return f"Время последнего приёма пищи исправлено на {occurred:%H:%M}."

    def _snooze(self, profile_id: UUID, text: str, source_key: str, now: datetime) -> str:
        match = re.search(r"/позже\s+(\d{1,3})", text.lower())
        if match is None or int(match.group(1)) <= 0:
            return "Укажите число минут, например: /позже 30."
        meal = self._latest_meal(profile_id)
        if meal is None:
            return "Сначала сохраните приём пищи."
        minutes = int(match.group(1))
        self._store.put(profile_id, "food", "control", source_key, {
            "action": "snooze", "meal_id": meal.id,
            "until": (now + timedelta(minutes=minutes)).isoformat(),
        }, at=now)
        return f"Напомню через {minutes} мин."

    def _control(self, profile_id: UUID, action: str, source_key: str, now: datetime) -> str:
        meal = self._latest_meal(profile_id)
        if meal is None:
            return "Сначала сохраните приём пищи."
        self._store.put(profile_id, "food", "control", source_key,
                        {"action": action, "meal_id": meal.id}, at=now)
        return "Этот интервал пропущен."

    def _setting(self, profile_id: UUID, source_key: str, enabled: bool, now: datetime) -> None:
        self._store.put(profile_id, "food", "settings", source_key,
                        {"reminders_enabled": enabled}, at=now)

    def _summary(self, profile_id: UUID, now: datetime, days: int) -> str:
        start = now.date() - timedelta(days=days - 1)
        meals = [m for m in self._store.list(profile_id, "food", "meal")
                 if start <= self._from_iso(m.payload["occurred_at"]).date() <= now.date()]
        if not meals:
            return "Сохранённых приёмов пищи нет; соблюдение плана неизвестно."
        known = sum(1 for m in meals if m.payload.get("analysis"))
        unknown = len(meals) - known
        categories = ", ".join(str(m.payload.get("category", "meal")) for m in reversed(meals))
        return (f"Сохранено приёмов пищи: {len(meals)} ({categories}). "
                f"Анализ доступен: {known}; неизвестно: {unknown}. "
                "Соблюдение интервалов оценивается только по сохранённым данным.")

    def _protocol(self, profile_id: UUID) -> dict[str, Any]:
        record = self._by_source(profile_id, "settings", "protocol")
        return dict(record.payload) if record else {}

    def _reminders_enabled(self, profile_id: UUID) -> bool:
        for record in self._store.list(profile_id, "food", "settings"):
            if "reminders_enabled" in record.payload:
                return bool(record.payload["reminders_enabled"])
        return True

    def _latest_meal(self, profile_id: UUID) -> Record | None:
        meals = self._store.list(profile_id, "food", "meal")
        if not meals:
            return None
        return max(meals, key=lambda record: self._from_iso(record.payload["occurred_at"]))

    def _latest_control(self, profile_id: UUID, meal_id: str) -> Record | None:
        return next((r for r in self._store.list(profile_id, "food", "control")
                     if r.payload.get("meal_id") == meal_id), None)

    def _by_source(self, profile_id: UUID, kind: str, source_key: str) -> Record | None:
        return next((r for r in self._store.list(profile_id, "food", kind)
                     if r.source_key == source_key), None)

    @staticmethod
    def _extract_time(text: str, now: datetime) -> tuple[datetime, str, bool]:
        match = _TIME.search(text)
        if match is None:
            return now, text, True
        occurred = datetime.combine(
            now.date(), time(int(match.group(1)), int(match.group(2))), tzinfo=now.tzinfo,
        )
        age = now - occurred
        valid = timedelta(minutes=-15) <= age <= timedelta(hours=18)
        return occurred, (text[:match.start()] + text[match.end():]).strip(), valid

    @staticmethod
    def _category(text: str, occurred: datetime) -> str:
        lowered = text.lower()
        labels = {
            "breakfast": ("завтрак", "breakfast"), "lunch": ("обед", "lunch"),
            "afternoon": ("полдник", "перекус", "snack"),
            "dinner": ("ужин", "dinner"),
        }
        for category, words in labels.items():
            if any(word in lowered for word in words):
                return category
        hour = occurred.hour
        if hour < 11:
            return "breakfast"
        if hour < 15:
            return "lunch"
        if hour < 18:
            return "afternoon"
        return "dinner"

    @staticmethod
    def _parse_analysis(raw: str, photo: bool) -> dict[str, Any] | None:
        try:
            cleaned = raw.strip()
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned)
            value = json.loads(cleaned)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(value, dict):
            return None
        result = dict(value)
        for key in _NUTRIENTS:
            number = result.get(key)
            if number is None:
                result[key] = None
            elif (not isinstance(number, (int, float)) or isinstance(number, bool)
                  or not math.isfinite(float(number)) or number < 0
                  or round(float(number), 1) != float(number)):
                result[key] = None
                result.setdefault("unknowns", []).append(f"{key}: нет надёжной оценки")
        confidence = result.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0 <= confidence <= 1:
            result["confidence"] = None
        result.setdefault("foods", [])
        result.setdefault("portion_estimate", None)
        result.setdefault("unknowns", [])
        if photo and not result["portion_estimate"]:
            result["unknowns"].append("размер порции по фото неизвестен")
        feedback = result.get("feedback")
        result["feedback"] = str(feedback).strip() if feedback else "Состав сохранён; деталей для наблюдения недостаточно."
        return result

    @staticmethod
    def _feedback(payload: dict[str, Any]) -> str:
        return str(payload["analysis"]["feedback"])

    @staticmethod
    def _quiet(now: datetime, protocol: dict[str, Any]) -> bool:
        quiet = protocol.get("quiet_hours", {})
        try:
            start = time.fromisoformat(str(quiet.get("start", "22:00")))
            end = time.fromisoformat(str(quiet.get("end", "07:00")))
        except (TypeError, ValueError):
            start, end = _QUIET_START, _QUIET_END
        current = now.timetz().replace(tzinfo=None)
        return current >= start or current < end if start > end else start <= current < end

    @staticmethod
    def _positive_number(value: Any) -> float | None:
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
            return float(value)
        return None

    @staticmethod
    def _from_iso(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        FoodCoach._aware(parsed)
        return parsed

    @staticmethod
    def _aware(value: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("now and stored times must be timezone-aware")

    @staticmethod
    def _system_prompt() -> str:
        return (
            "Return JSON only with foods, portion_estimate, nullable kcal, protein_g, fat_g, "
            "carbs_g, saturated_fat_g, fiber_g, cholesterol_mg, confidence, unknowns, feedback. "
            "Never invent quantities. Missing nutrients are null. A photo portion is an estimate. "
            "Feedback is short and neutral: one plate observation and at most one optional change "
            "relative to the supplied current protocol. Do not call meal timing a physiological law "
            "or claim yolks or dairy are universally forbidden."
        )
