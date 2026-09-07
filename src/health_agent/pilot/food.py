"""Durable food capture and meal-interval reminders for the pilot."""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import Attachment, Brain, Notice, Record, Store
from health_agent.pilot.food_history import build_food_history

_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
_NUTRIENTS = (
    "kcal", "protein_g", "fat_g", "carbs_g", "saturated_fat_g", "fiber_g",
    "cholesterol_mg",
)
_QUIET_START = time(22, 0)
_QUIET_END = time(7, 0)
_USER_ZONE = ZoneInfo("Europe/Moscow")
_COMPONENT_LABELS = {
    "vegetables": "овощи", "protein": "источник белка", "grains": "крупы или хлеб",
    "fruit": "фрукты", "dairy": "молочный продукт",
}
_COMPONENT_TERMS = {
    "vegetables": ("овощ", "салат", "зелень", "томат", "огур", "капуст", "vegetable"),
    "protein": ("мяс", "рыб", "яйц", "кур", "индей", "боб", "тофу", "protein"),
    "grains": ("рис", "греч", "овся", "хлеб", "паст", "макарон", "grain"),
    "fruit": ("фрукт", "яблок", "банан", "ягод", "малин", "клубник", "fruit"),
    "dairy": ("молок", "йогур", "кефир", "сыр", "творог", "dairy"),
}


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
        if lowered.startswith("/порция"):
            return self._correct_portion(profile_id, command, source_key, now)
        if lowered.startswith("/сегодня"):
            return self._summary(profile_id, now, days=1)
        if lowered.startswith("/неделя"):
            return self._weekly_text(profile_id, now)
        if not command and attachment is None:
            return "Опишите приём пищи или приложите фотографию."
        if attachment is not None:
            return self._photo(profile_id, command, source_key, now, attachment)
        saved_comment = self._by_source(profile_id, "comment", source_key)
        if saved_comment is not None:
            meal = self._store.get(profile_id, str(saved_comment.payload["meal_id"]))
            if meal is None:
                return "Комментарий сохранён, но связанный приём пищи не найден."
            return self._comment(profile_id, meal, command, source_key, now)
        saved_photo_confirmation = self._by_source(profile_id, "photo_confirmation", source_key)
        if saved_photo_confirmation is not None:
            return self._confirm_photo(
                profile_id, str(saved_photo_confirmation.payload["decision"]), source_key, now,
            )
        if self._by_source(profile_id, "text_confirmation", source_key) is not None:
            return self._confirm_text(profile_id, source_key, now)
        if lowered in {"тот же", "новый"} and (
            self._pending_photo(profile_id) is not None
            or self._by_source(profile_id, "photo_confirmation", source_key) is not None
        ):
            return self._confirm_photo(profile_id, lowered, source_key, now)
        if lowered == "новый" and (
            self._pending_text(profile_id) is not None
            or self._by_source(profile_id, "text_confirmation", source_key) is not None
        ):
            return self._confirm_text(profile_id, source_key, now)
        if self._clear_meal(command):
            return self._meal(profile_id, command, source_key, now, None)
        candidate = self._comment_candidate(profile_id, now)
        if candidate is not None and (not self._is_question(command) or self._plate_question(command)):
            return self._comment(profile_id, candidate, command, source_key, now)
        if self._is_question(command):
            return "С общими вопросами лучше обратиться в основной Health Agent. Здесь я сохраняю питание."
        self._store.put(profile_id, "food", "pending_text", source_key, {"text": command}, at=now)
        return "Это новый приём пищи? Напишите «новый», если хотите сохранить его как приём."

    def due(self, profile_id: UUID, now: datetime) -> list[Notice]:
        self._aware(now)
        if not self._reminders_enabled(profile_id):
            return []
        notices: list[Notice] = []
        meal = self._latest_meal(profile_id)
        if meal is not None and meal.payload.get("category") != "dinner":
            reminder = self._meal_notice(profile_id, meal, now)
            if reminder is not None:
                notices.append(reminder)
        weekly = self._weekly_notice(profile_id, now)
        if weekly is not None:
            notices.append(weekly)
        return notices

    def _meal_notice(self, profile_id: UUID, meal: Record, now: datetime) -> Notice | None:
        anchor = self._from_iso(str(meal.payload.get("ended_at", meal.payload["occurred_at"])))
        protocol = self._protocol(profile_id)
        configured = self._positive_number(protocol.get("interval_hours"))
        hours = configured if configured is not None and 2.5 <= configured <= 4.5 else 3.5
        target = anchor + timedelta(hours=hours)
        expires = anchor + timedelta(hours=4.5)
        control = self._latest_control(profile_id, meal.id)
        if control is not None:
            action = control.payload.get("action")
            if action == "skip":
                return None
            if action == "snooze":
                target = self._from_iso(control.payload["until"])
        if target > expires or now < target or now > expires or self._quiet(now, protocol):
            return None
        key = f"meal:{meal.id}:at:{target.astimezone(UTC).isoformat()}"
        # By contract the root writes a notice record only after successful delivery.
        if any(r.source_key == key for r in self._store.list(profile_id, "food", "notice")):
            return None
        return Notice(key, "Плановый интервал после последнего приёма пищи прошёл. Хотите поесть сейчас?")

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
                "analysis_status": "pending",
            }
            existing = self._store.put(
                profile_id, "food", "meal", source_key, payload, at=occurred,
            )
        updated = self._analyse(profile_id, existing)
        if updated.payload.get("analysis") is None:
            return "Приём пищи сохранён; анализ сейчас недоступен. Напоминание продолжит работать."
        return self._feedback(updated.payload)

    def _photo(
        self, profile_id: UUID, text: str, source_key: str, now: datetime,
        attachment: Attachment,
    ) -> str:
        saved = self._by_source(profile_id, "photo", source_key)
        if saved is not None:
            if saved.payload.get("status") == "pending":
                return "Это продолжение того же приёма или новый приём?"
            meal = self._store.get(profile_id, str(saved.payload.get("meal_id")))
            if meal is None:
                return "Фото сохранено, но связанный приём пищи не найден."
            if meal.payload.get("analysis_status") == "complete":
                return self._feedback(meal.payload)
            updated = self._analyse(
                profile_id, meal, image_path=Path(str(saved.payload["path"])),
            )
            return self._analysis_reply(updated)

        latest = self._photo_candidate(profile_id, now)
        if latest is None or not latest.payload.get("latest_photo_at"):
            return self._start_photo_meal(profile_id, text, source_key, now, attachment)
        gap = now - self._from_iso(str(latest.payload["latest_photo_at"]))
        photo_payload = {
            "path": str(attachment.path), "caption": attachment.caption,
            "event_at": now.isoformat(), "candidate_meal_id": latest.id,
            "meal_id": None, "status": "pending",
        }
        photo = self._store.put(
            profile_id, "food", "photo", source_key, photo_payload, at=now,
        )
        if gap < timedelta(minutes=40):
            return self._attach_photo(profile_id, latest, photo)
        if gap > timedelta(minutes=150):
            return self._start_photo_meal(profile_id, text, source_key, now, attachment, photo)
        return "Это продолжение того же приёма или новый приём?"

    def _start_photo_meal(
        self, profile_id: UUID, text: str, source_key: str, now: datetime,
        attachment: Attachment, photo: Record | None = None,
    ) -> str:
        photo = photo or self._store.put(profile_id, "food", "photo", source_key, {
            "path": str(attachment.path), "caption": attachment.caption,
            "event_at": now.isoformat(), "candidate_meal_id": None,
            "meal_id": None, "status": "pending",
        }, at=now)
        occurred, cleaned, valid = self._extract_time(text, now)
        if not valid:
            return "Уточните дату или время приёма пищи: указанное время выглядит будущим или слишком давним."
        item = self._photo_item(photo)
        meal = self._store.put(profile_id, "food", "meal", source_key, {
            "original": text, "caption": attachment.caption,
            "photo_path": str(attachment.path), "photos": [item],
            "latest_photo_at": now.isoformat(),
            "ended_at": (now + timedelta(minutes=20)).isoformat(),
            "end_source": "last_photo_plus_20m",
            "occurred_at": occurred.isoformat(), "captured_at": now.isoformat(),
            "category": self._category(cleaned or attachment.caption, occurred),
            "category_source": "labelled_heuristic", "analysis": None,
            "analysis_raw": None, "analysis_error": None, "analysis_status": "pending",
        }, at=occurred)
        self._store.patch(profile_id, photo.id, {**photo.payload, "meal_id": meal.id, "status": "confirmed"})
        return self._analysis_reply(self._analyse(profile_id, meal, image_path=attachment.path))

    def _attach_photo(self, profile_id: UUID, meal: Record, photo: Record) -> str:
        event_at = self._from_iso(str(photo.payload["event_at"]))
        previous_anchor = self._from_iso(str(meal.payload.get("latest_photo_at", event_at.isoformat())))
        latest_anchor = max(previous_anchor, event_at)
        payload = dict(meal.payload)
        payload["photos"] = [*payload.get("photos", []), self._photo_item(photo)]
        payload["latest_photo_at"] = latest_anchor.isoformat()
        payload["ended_at"] = (latest_anchor + timedelta(minutes=20)).isoformat()
        payload["end_source"] = "last_photo_plus_20m"
        payload["previous_analysis"] = payload.get("analysis")
        persisted = self._store.patch(profile_id, meal.id, payload)
        self._store.patch(profile_id, photo.id, {**photo.payload, "meal_id": meal.id, "status": "confirmed"})
        return self._analysis_reply(
            self._analyse(profile_id, persisted, image_path=Path(str(photo.payload["path"])))
        )

    def _confirm_photo(self, profile_id: UUID, decision: str, source_key: str, now: datetime) -> str:
        previous = self._by_source(profile_id, "photo_confirmation", source_key)
        if previous is not None:
            meal = self._store.get(profile_id, str(previous.payload["meal_id"]))
            if meal is None:
                return "Подтверждение сохранено."
            if meal.payload.get("analysis_status") != "complete":
                photo = self._by_source(
                    profile_id, "photo", str(previous.payload["photo_source_key"]),
                )
                image = Path(str(photo.payload["path"])) if photo is not None else None
                meal = self._analyse(profile_id, meal, image_path=image)
            return self._analysis_reply(meal)
        photo = self._pending_photo(profile_id)
        if photo is None:
            return "Не нашёл фото, которое ждёт уточнения."
        if decision == "тот же":
            meal = self._store.get(profile_id, str(photo.payload["candidate_meal_id"]))
            if meal is None:
                return "Предыдущий приём пищи не найден; фото сохранено отдельно."
            reply = self._attach_photo(profile_id, meal, photo)
        else:
            attachment = Attachment(Path(str(photo.payload["path"])), "image/jpeg", str(photo.payload.get("caption", "")))
            reply = self._start_photo_meal(profile_id, "", photo.source_key, self._from_iso(str(photo.payload["event_at"])), attachment, photo)
            meal = self._store.get(profile_id, str(self._by_source(profile_id, "photo", photo.source_key).payload["meal_id"]))  # type: ignore[union-attr]
        assert meal is not None
        self._store.put(profile_id, "food", "photo_confirmation", source_key, {
            "photo_source_key": photo.source_key, "meal_id": meal.id, "decision": decision,
        }, at=now)
        return reply

    def _comment(
        self, profile_id: UUID, meal: Record, text: str, source_key: str, now: datetime,
    ) -> str:
        saved = self._by_source(profile_id, "comment", source_key)
        if saved is not None:
            bound = self._store.get(profile_id, str(saved.payload["meal_id"]))
            if bound is None:
                return "Комментарий сохранён, но связанный приём пищи не найден."
            if bound.payload.get("analysis_status") == "complete":
                return self._feedback(bound.payload)
            return self._analysis_reply(self._analyse(profile_id, bound))
        saved = self._store.put(profile_id, "food", "comment", source_key, {
            "meal_id": meal.id, "text": text,
        }, at=now)
        payload = dict(meal.payload)
        payload["previous_analysis"] = payload.get("analysis")
        if re.search(r"\b(?:порци|примерно|около)\b", text.lower()):
            payload["user_portion"] = text[:100]
        persisted = self._store.patch(profile_id, meal.id, payload)
        return self._analysis_reply(self._analyse(profile_id, persisted))

    def _confirm_text(self, profile_id: UUID, source_key: str, now: datetime) -> str:
        previous = self._by_source(profile_id, "text_confirmation", source_key)
        if previous is not None:
            meal = self._store.get(profile_id, str(previous.payload["meal_id"]))
            if meal is None:
                return "Подтверждение сохранено."
            if meal.payload.get("analysis_status") != "complete":
                meal = self._analyse(profile_id, meal)
            return self._analysis_reply(meal)
        pending = self._pending_text(profile_id)
        if pending is None:
            return "Не нашёл описание, которое ждёт уточнения."
        reply = self._meal(
            profile_id, str(pending.payload["text"]), pending.source_key, pending.at, None,
        )
        meal = self._by_source(profile_id, "meal", pending.source_key)
        assert meal is not None
        self._store.patch(profile_id, pending.id, {**pending.payload, "status": "confirmed"})
        self._store.put(profile_id, "food", "text_confirmation", source_key, {
            "pending_source_key": pending.source_key, "meal_id": meal.id,
        }, at=now)
        return reply

    @staticmethod
    def _photo_item(photo: Record) -> dict[str, Any]:
        return {"source_key": photo.source_key, "path": photo.payload["path"],
                "caption": photo.payload.get("caption", ""),
                "event_at": photo.payload["event_at"], "analysis": None,
                "analysis_raw": None}

    @staticmethod
    def _analysis_reply(meal: Record) -> str:
        if meal.payload.get("analysis") is None:
            return "Приём пищи сохранён; анализ сейчас недоступен. Напоминание продолжит работать."
        return FoodCoach._feedback(meal.payload)

    def _analyse(
        self, profile_id: UUID, meal: Record, image_path: Path | None = None,
    ) -> Record:
        payload = dict(meal.payload)
        payload["analysis"] = None
        payload["analysis_raw"] = None
        payload["analysis_error"] = None
        payload["analysis_status"] = "pending"
        try:
            comments = [
                str(item.payload["text"])
                for item in reversed(self._store.list(profile_id, "food", "comment"))
                if item.payload.get("meal_id") == meal.id
            ]
            chosen_image = image_path
            if chosen_image is None and payload.get("photos"):
                chosen_image = Path(str(payload["photos"][-1]["path"]))
            if chosen_image is None and payload.get("photo_path"):
                chosen_image = Path(str(payload["photo_path"]))
            chosen_caption = payload["caption"]
            if chosen_image is not None:
                chosen_caption = next(
                    (str(item.get("caption", "")) for item in reversed(payload.get("photos", []))
                     if Path(str(item["path"])) == chosen_image),
                    chosen_caption,
                )
            raw = self._brain(
                self._system_prompt(),
                {"meal": {
                    "text": payload["original"], "caption": chosen_caption,
                    "portion": payload.get("user_portion"), "comments": comments,
                    "previous_analysis": payload.get("previous_analysis"),
                    "photo_analyses": [item.get("analysis") for item in payload.get("photos", [])
                                       if item.get("analysis") is not None],
                },
                 "protocol": self._protocol(profile_id)},
                image_path=chosen_image,
            )
            payload["analysis_raw"] = raw
            derived = self._parse_analysis(
                raw, bool(payload["photo_path"]), str(payload["category"]),
                self._protocol(profile_id), payload.get("user_portion"),
            )
            payload["analysis"] = derived
            payload["analysis_error"] = None if payload["analysis"] is not None else "invalid_json"
            payload["analysis_status"] = ("complete" if payload["analysis"] is not None
                                          else "incomplete")
            if chosen_image is not None and derived is not None:
                photos = [dict(item) for item in payload.get("photos", [])]
                for item in reversed(photos):
                    if Path(str(item["path"])) == chosen_image and item.get("analysis") is None:
                        item["analysis_raw"] = raw
                        item["analysis"] = derived
                        break
                payload["photos"] = photos
            if derived is not None:
                observations = [item["analysis"] for item in payload.get("photos", [])
                                if isinstance(item.get("analysis"), dict)]
                payload["analysis"] = self._aggregate_analysis(
                    [*observations, derived], str(payload["category"]),
                    self._protocol(profile_id), payload.get("user_portion"),
                )
                payload["analysis_revisions"] = [*payload.get("analysis_revisions", []), {
                    "raw": raw, "derived": derived,
                    "reason": "photo" if chosen_image is not None else "text",
                }]
        except Exception as exc:  # noqa: BLE001 - authorized model callable is a boundary
            payload["analysis"] = None
            payload["analysis_error"] = type(exc).__name__
            payload["analysis_status"] = "incomplete"
        return self._store.patch(profile_id, meal.id, payload)

    @staticmethod
    def _aggregate_analysis(
        values: list[dict[str, Any]], category: str, protocol: dict[str, Any],
        user_portion: Any,
    ) -> dict[str, Any]:
        latest = dict(values[-1])
        foods: list[str] = []
        seen: set[str] = set()
        components: set[str] = set()
        unknowns: list[str] = []
        for value in values:
            for food in value.get("foods", []):
                normalized = str(food).strip().casefold()
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    foods.append(str(food).strip())
            components.update(value.get("plate_components", []))
            for unknown in value.get("unknowns", []):
                if unknown not in unknowns:
                    unknowns.append(unknown)
        latest["foods"] = foods[:50]
        latest["plate_components"] = [key for key in _COMPONENT_LABELS if key in components]
        latest["unknowns"] = unknowns[:50]
        if len(values) > 2:
            latest["portion_estimate"] = None
            for key in _NUTRIENTS:
                latest[key] = None
            latest["unknowns"].append(
                "несколько фото могут показывать разные части или повторный вид; количества не суммированы"
            )
        latest["portion_user"] = user_portion if isinstance(user_portion, str) else None
        latest["feedback"] = FoodCoach._render_feedback(latest, category, protocol)
        return latest

    def _correct(self, profile_id: UUID, text: str, source_key: str, now: datetime) -> str:
        previous = self._by_source(profile_id, "correction", source_key)
        if previous is not None:
            occurred = self._from_iso(previous.payload["occurred_at"])
            return f"Время приёма пищи уже исправлено на {occurred.astimezone(_USER_ZONE):%H:%M}."
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
        return f"Время последнего приёма пищи исправлено на {occurred.astimezone(_USER_ZONE):%H:%M}."

    def _snooze(self, profile_id: UUID, text: str, source_key: str, now: datetime) -> str:
        match = re.search(r"/позже\s+(\d{1,3})", text.lower())
        if match is None or int(match.group(1)) <= 0:
            return "Укажите число минут, например: /позже 30."
        meal = self._latest_meal(profile_id)
        if meal is None:
            return "Сначала сохраните приём пищи."
        minutes = int(match.group(1))
        target = now + timedelta(minutes=minutes)
        occurred = self._from_iso(str(meal.payload.get("ended_at", meal.payload["occurred_at"])))
        protocol = self._protocol(profile_id)
        if target > occurred + timedelta(hours=4.5):
            return "Не могу отложить: интервал напоминания уже закончится. Новое напоминание не запланировано."
        if self._quiet(target, protocol):
            return "Не могу отложить на тихие часы. Новое напоминание не запланировано."
        self._store.put(profile_id, "food", "control", source_key, {
            "action": "snooze", "meal_id": meal.id,
            "until": target.isoformat(),
        }, at=now)
        return f"Напомню через {minutes} мин."

    def _correct_portion(
        self, profile_id: UUID, text: str, source_key: str, now: datetime,
    ) -> str:
        previous = self._by_source(profile_id, "portion_correction", source_key)
        if previous is not None:
            meal = self._store.get(profile_id, str(previous.payload["meal_id"]))
            if meal is None:
                return "Порция сохранена, но связанный приём пищи не найден."
            complete = (meal.payload.get("analysis_status") == "complete"
                        or (meal.payload.get("analysis") is not None
                            and meal.payload.get("analysis_error") is None))
            if complete:
                return self._feedback(meal.payload)
            payload = dict(meal.payload)
            payload["user_portion"] = previous.payload["portion"]
            payload["portion_analysis_source_key"] = source_key
            persisted = self._store.patch(profile_id, meal.id, payload)
            updated = self._analyse(profile_id, persisted)
            if updated.payload.get("analysis_status") != "complete":
                return "Порция сохранена; повторный анализ сейчас недоступен."
            return self._feedback(updated.payload)
        portion = text[len("/порция"):].strip()
        if not portion or len(portion) > 100:
            return "Укажите порцию, например: /порция 200 г."
        meal = self._latest_meal(profile_id)
        if meal is None:
            return "Сначала сохраните приём пищи."
        correction = self._store.put(
            profile_id, "food", "portion_correction", source_key,
            {"meal_id": meal.id, "portion": portion}, at=now,
        )
        payload = dict(meal.payload)
        payload["previous_analysis"] = payload.get("analysis")
        payload["previous_analysis_raw"] = payload.get("analysis_raw")
        payload["user_portion"] = correction.payload["portion"]
        payload["portion_corrected_at"] = now.isoformat()
        payload["portion_analysis_source_key"] = source_key
        payload["analysis"] = None
        payload["analysis_raw"] = None
        payload["analysis_error"] = None
        payload["analysis_status"] = "pending"
        persisted = self._store.patch(profile_id, meal.id, payload)
        updated = self._analyse(profile_id, persisted)
        if updated.payload.get("analysis_status") != "complete":
            return "Порция сохранена; повторный анализ сейчас недоступен."
        return self._feedback(updated.payload)

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
        local_now = now.astimezone(_USER_ZONE)
        start = local_now.date() - timedelta(days=days - 1)
        meals = [m for m in self._store.list(profile_id, "food", "meal")
                 if start <= self._from_iso(m.payload["occurred_at"]).astimezone(_USER_ZONE).date()
                 <= local_now.date()]
        if not meals:
            return "Сохранённых приёмов пищи нет; соблюдение плана неизвестно."
        known = sum(1 for m in meals if m.payload.get("analysis"))
        unknown = len(meals) - known
        ordered = sorted(meals, key=lambda m: self._from_iso(m.payload["occurred_at"]))
        categories = ", ".join(str(m.payload.get("category", "meal")) for m in ordered)
        intervals = []
        closed_days = set()
        for previous, current in pairwise(ordered):
            previous_at = self._from_iso(previous.payload["occurred_at"])
            current_at = self._from_iso(current.payload["occurred_at"])
            previous_day = previous_at.astimezone(_USER_ZONE).date()
            current_day = current_at.astimezone(_USER_ZONE).date()
            if previous.payload.get("category") == "dinner":
                closed_days.add(previous_day)
            if previous_day == current_day and previous_day not in closed_days:
                intervals.append((current_at - previous_at).total_seconds() / 3600)
        adherent = sum(2.5 <= interval <= 4.5 for interval in intervals)
        adherence = (f"Интервалы в плане: {adherent} из {len(intervals)}."
                     if intervals else "Для оценки интервалов пока недостаточно записей.")
        return (f"Сохранено приёмов пищи: {len(meals)} ({categories}). "
                f"Анализ доступен: {known}; неизвестно: {unknown}. "
                f"{adherence} Оценка основана только на сохранённых данных.")

    def _weekly_notice(self, profile_id: UUID, now: datetime) -> Notice | None:
        local = now.astimezone(_USER_ZONE)
        if local.weekday() != 6 or local.time().replace(tzinfo=None) < time(18, 0):
            return None
        if self._quiet(now, self._protocol(profile_id)):
            return None
        iso_year, iso_week, _ = local.isocalendar()
        key = f"food-weekly-{iso_year}-W{iso_week}"
        if self._by_source(profile_id, "notice", key) is not None:
            return None
        cached = self._by_source(profile_id, "weekly_reflection", key)
        if cached is not None:
            return Notice(key, str(cached.payload["text"]))
        text = self._weekly_text(profile_id, now)
        if text.startswith("Сохранённых приёмов пищи нет"):
            return None
        history = build_food_history(self._store, profile_id, now, days=7)
        self._store.put(profile_id, "food", "weekly_reflection", key, {
            "text": text, "facts": history,
        }, at=now)
        return Notice(key, text)

    def _weekly_text(self, profile_id: UUID, now: datetime) -> str:
        history = build_food_history(self._store, profile_id, now, days=7)
        if history["recorded_meal_count"] == 0:
            return "Сохранённых приёмов пищи нет; соблюдение плана неизвестно."
        fallback = self._weekly_facts(history, self._protocol(profile_id))
        text = fallback
        try:
            suggestion = self._brain(
                "Кратко по-русски: одна осторожная идея следующего шага по журналу питания. "
                "Учитывайте регулярность, разнообразие и заданную рамку тарелки. Не считайте "
                "неполный журнал полным рационом, не ставьте диагнозов и не выдумывайте числа.",
                {"recorded_food_history": history, "protocol": self._protocol(profile_id)},
                image_path=None,
            ).strip()
            if self._safe_weekly_suggestion(suggestion):
                text = f"{fallback} Возможная идея: {suggestion}"[:1200]
        except Exception:  # noqa: BLE001 - stable factual fallback at provider boundary
            text = fallback
        return text

    @staticmethod
    def _safe_weekly_suggestion(value: str) -> bool:
        lowered = value.casefold()
        forbidden = ("диабет", "рак", "лекарств", "запрещ", "голода", "обязательно")
        return bool(value) and len(value) <= 240 and "{" not in value and not any(
            word in lowered for word in forbidden
        )

    @staticmethod
    def _weekly_facts(history: dict[str, Any], protocol: dict[str, Any]) -> str:
        meals = int(history["recorded_meal_count"])
        days = len(history["recorded_days"])
        nutrient_counts = {
            key: sum(meal["nutrients_estimated"].get(key) is not None
                     for meal in history["meals"])
            for key in _NUTRIENTS
        }
        ordered = sorted(history["meals"], key=lambda meal: meal["recorded_at"])
        intervals: list[float] = []
        for recorded_day in history["recorded_days"]:
            day_meals = [meal for meal in ordered if FoodCoach._from_iso(
                meal["recorded_at"]).astimezone(_USER_ZONE).date().isoformat() == recorded_day]
            closed = False
            previous: dict[str, Any] | None = None
            for current in day_meals:
                if previous is not None and not closed:
                    previous_end = FoodCoach._from_iso(
                        previous.get("ended_at", previous["recorded_at"]),
                    )
                    current_at = FoodCoach._from_iso(current["recorded_at"])
                    interval = (current_at - previous_end).total_seconds() / 3600
                    if interval >= 0:
                        intervals.append(interval)
                if current.get("category") == "dinner":
                    closed = True
                previous = current
        interval_note = (
            f"Интервалы в плане: {sum(2.5 <= value <= 4.5 for value in intervals)} "
            f"из {len(intervals)} (между окончанием и следующим приёмом внутри дня)."
            if intervals else "Для оценки интервалов внутри дня записей недостаточно."
        )
        known = ", ".join(f"{key}={count}" for key, count in nutrient_counts.items())
        foods = sorted({food for meal in history["meals"] for food in meal["foods"]}, key=str.casefold)
        variety = f"Разнообразие по записям: {len(foods)} позиций"
        if foods:
            variety += f" ({', '.join(foods[:8])})"
        rules = protocol.get("plate_rules")
        if isinstance(rules, dict) and rules:
            eligible = 0
            matched = 0
            for meal in history["meals"]:
                expected = FoodCoach._expected_components(protocol, str(meal.get("category", "")))
                if expected:
                    eligible += 1
                    observed = FoodCoach._components({"foods": meal["foods"]})
                    matched += expected <= observed
            framework = (
                f"По рамке тарелки видны все заданные компоненты в {matched} из {eligible} записей"
                if eligible else "Рамка тарелки настроена, но сопоставимых записей нет"
            )
        else:
            framework = "Рамка тарелки не настроена"
        dates = ", ".join(history["recorded_days"])
        return (
            f"Сохранено {meals} приёмов за {days} дней ({dates}). {interval_note} "
            f"{variety}. {framework}. "
            f"Число записей с известными оценками нутриентов: {known}. "
            "Это только записи, а не полный рацион; пропуски не означают голодание. "
            "Следующий шаг: продолжать отмечать приёмы фото и короткими уточнениями."
        )

    def _pending_photo(self, profile_id: UUID) -> Record | None:
        return next((item for item in self._store.list(profile_id, "food", "photo")
                     if item.payload.get("status") == "pending"), None)

    def _photo_candidate(self, profile_id: UUID, event_at: datetime) -> Record | None:
        candidates = [
            meal for meal in self._store.list(profile_id, "food", "meal")
            if meal.payload.get("latest_photo_at")
            and self._from_iso(str(meal.payload["latest_photo_at"])) <= event_at
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda meal: self._from_iso(str(meal.payload["latest_photo_at"])))

    def _pending_text(self, profile_id: UUID) -> Record | None:
        return next((item for item in self._store.list(profile_id, "food", "pending_text")
                     if item.payload.get("status", "pending") == "pending"), None)

    def _comment_candidate(self, profile_id: UUID, now: datetime) -> Record | None:
        meal = self._latest_meal(profile_id)
        if meal is None:
            return None
        anchor = self._from_iso(str(meal.payload.get("latest_photo_at", meal.payload["occurred_at"])))
        if anchor.astimezone(_USER_ZONE).date() != now.astimezone(_USER_ZONE).date():
            return None
        return meal if timedelta(0) <= now - anchor <= timedelta(hours=4.5) else None

    @staticmethod
    def _is_question(text: str) -> bool:
        lowered = text.lower().strip()
        return "?" in text or lowered.startswith(("почему", "как ", "что делать", "можно ли"))

    @staticmethod
    def _plate_question(text: str) -> bool:
        lowered = text.lower()
        return any(word in lowered for word in ("тарел", "блюд", "здесь", "это", "порци"))

    @staticmethod
    def _clear_meal(text: str) -> bool:
        lowered = text.lower().strip()
        if lowered.startswith("/ел"):
            return True
        if re.search(r"\b(?:поел|съел|позавтракал|пообедал|поужинал)\b", lowered):
            return True
        return bool(re.search(
            r"^(?:завтрак|обед|ужин|перекус)\s*(?::|-)?\s+\S+", lowered,
        ))

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
        local_now = now.astimezone(_USER_ZONE)
        local_occurred = datetime.combine(
            local_now.date(), time(int(match.group(1)), int(match.group(2))),
            tzinfo=_USER_ZONE,
        )
        occurred = local_occurred.astimezone(UTC)
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
        hour = occurred.astimezone(_USER_ZONE).hour
        if hour < 11:
            return "breakfast"
        if hour < 15:
            return "lunch"
        if hour < 18:
            return "afternoon"
        return "dinner"

    @staticmethod
    def _parse_analysis(
        raw: str, photo: bool, category: str = "", protocol: dict[str, Any] | None = None,
        user_portion: Any = None,
    ) -> dict[str, Any] | None:
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
        foods = result.get("foods")
        result["foods"] = ([item.strip() for item in foods
                            if isinstance(item, str) and item.strip()][:50]
                           if isinstance(foods, list) else [])
        portion = result.get("portion_estimate")
        result["portion_estimate"] = portion.strip()[:200] if isinstance(portion, str) and portion.strip() else None
        result["portion_user"] = (user_portion.strip()[:100]
                                  if isinstance(user_portion, str) and user_portion.strip()
                                  else None)
        unknowns = result.get("unknowns")
        result["unknowns"] = ([item.strip() for item in unknowns
                               if isinstance(item, str) and item.strip()][:50]
                              if isinstance(unknowns, list) else [])
        supplied_components = result.get("plate_components")
        if isinstance(supplied_components, list):
            result["plate_components"] = [
                item for item in supplied_components
                if isinstance(item, str) and item in _COMPONENT_LABELS
            ][:5]
        elif isinstance(supplied_components, dict):
            result["plate_components"] = [
                key for key, evidence in supplied_components.items()
                if key in _COMPONENT_LABELS and isinstance(evidence, str) and evidence.strip()
            ][:5]
        else:
            result["plate_components"] = []
        for key in _NUTRIENTS:
            number = result.get(key)
            if number is None:
                result[key] = None
            elif (not isinstance(number, (int, float)) or isinstance(number, bool)
                  or not math.isfinite(float(number)) or number < 0
                  or round(float(number), 1) != float(number)):
                result[key] = None
                result["unknowns"].append(f"{key}: нет надёжной оценки")
        confidence = result.get("confidence")
        if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
                or not math.isfinite(float(confidence)) or not 0 <= confidence <= 1):
            result["confidence"] = None
        if photo and not result["portion_estimate"]:
            result["unknowns"].append("размер порции по фото неизвестен")
        # Model prose is evidence for audit/replay, never directly user-visible.
        result["feedback_untrusted"] = result.get("feedback")
        result["feedback"] = FoodCoach._render_feedback(result, category, protocol or {})
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
        current = now.astimezone(_USER_ZONE).timetz().replace(tzinfo=None)
        return current >= start or current < end if start > end else start <= current < end

    @staticmethod
    def _render_feedback(
        analysis: dict[str, Any], category: str, protocol: dict[str, Any],
    ) -> str:
        components = FoodCoach._components(analysis)
        if not components:
            return "Не удалось надёжно определить состав и порцию; уточните ингредиенты и размер порции."
        labels = [_COMPONENT_LABELS[item] for item in _COMPONENT_LABELS if item in components]
        observation = f"В записи отмечены: {', '.join(labels)}."
        expected = FoodCoach._expected_components(protocol, category)
        missing = next((item for item in _COMPONENT_LABELS
                        if item in expected and item not in components), None)
        if missing is not None:
            return f"{observation} По выбранному правилу можно добавить {_COMPONENT_LABELS[missing]}."
        if analysis.get("portion_estimate") is None and analysis.get("portion_user") is None:
            return f"{observation} Размер порции по записи неизвестен; оценки приблизительны."
        return observation

    @staticmethod
    def _components(analysis: dict[str, Any]) -> set[str]:
        supplied = analysis.get("plate_components")
        components = ({item for item in supplied if item in _COMPONENT_LABELS}
                      if isinstance(supplied, list) else set())
        for food in analysis.get("foods", []):
            lowered = food.lower()
            components.update(name for name, terms in _COMPONENT_TERMS.items()
                              if any(term in lowered for term in terms))
        return components

    @staticmethod
    def _expected_components(protocol: dict[str, Any], category: str) -> set[str]:
        rules = protocol.get("plate_rules")
        selected = rules.get(category) if isinstance(rules, dict) else None
        if isinstance(selected, list):
            text = " ".join(str(item) for item in selected)
        elif isinstance(selected, str):
            text = selected
        else:
            return set()
        lowered = text.lower()
        return {name for name, terms in _COMPONENT_TERMS.items()
                if name in lowered or any(term in lowered for term in terms)}

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
            "Return one JSON object only. plate_components MUST be a JSON array using only "
            "these exact strings: [\"vegetables\", \"protein\", \"grains\", \"fruit\", "
            "\"dairy\"]. Also return foods, portion_estimate, nullable kcal, protein_g, fat_g, "
            "carbs_g, saturated_fat_g, fiber_g, cholesterol_mg, confidence, unknowns, feedback. "
            "Never invent quantities. Missing nutrients are null. A photo portion is an estimate. "
            "Feedback is short and neutral: one plate observation and at most one optional change "
            "relative to the supplied current protocol. Do not call meal timing a physiological law "
            "or claim yolks or dairy are universally forbidden."
        )
