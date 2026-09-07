"""Persistent, restart-safe sleep diary and conversation coach."""

from __future__ import annotations

import re
from datetime import datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import (
    Attachment,
    Brain,
    HealthContext,
    Notice,
    Record,
    Store,
)

_MOSCOW = ZoneInfo("Europe/Moscow")
_MORNING_RE = re.compile(r"^/утро(?:\s+(\S+))?\s*$", re.IGNORECASE)
_DIARY_RE = re.compile(r"^/сон(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
_QUESTION_RE = re.compile(
    r"(?:\?|^(?:кто|что|где|когда|зачем|почему|как|можно|может ли|стоит ли|"
    r"нужно ли|нужен ли|нужна ли|нормально ли|опасно ли)(?:\s|$))",
    re.IGNORECASE,
)
_DREAM_RE = re.compile(
    r"\b((?:мне\s+)?(?:снил(?:ся|ась|ось|ись)|приснил(?:ся|ась|ось|ись))\b.*)$",
    re.IGNORECASE,
)


class SleepCoach:
    """A subjective sleep diary with bounded model context and local schedules."""

    def __init__(
        self,
        store: Store,
        brain: Brain,
        *,
        health_context: HealthContext | None = None,
    ) -> None:
        self.store = store
        self.brain = brain
        self.health_context = health_context

    def handle(
        self,
        profile_id: UUID,
        text: str,
        *,
        source_key: str,
        now: datetime,
        attachment: Attachment | None = None,
    ) -> str:
        now = _aware(now)
        stripped = text.strip()
        if not stripped:
            if attachment is not None and attachment.media_type.startswith("audio/"):
                return "Не вижу расшифровку голосового сообщения. Пришлите текст — запись не была выдумана или сохранена пустой."
            return "Напишите, как спалось, или используйте /сон текст."

        schedule_match = _MORNING_RE.match(stripped)
        if schedule_match:
            return self._set_schedule(profile_id, schedule_match.group(1), source_key, now)
        if stripped.lower() == "/дневник":
            return self._render_diary(profile_id)
        if stripped.lower() == "/итоги":
            return self._weekly_summary(profile_id, source_key, now)

        existing = self._turn(profile_id, f"assistant:{source_key}")
        if existing is not None:
            return str(existing.payload["text"])

        # On an incomplete retry, the first durable user text is authoritative.
        user_turn = self.store.put(
            profile_id,
            "sleep",
            "turn",
            f"user:{source_key}",
            {"role": "user", "text": stripped},
            at=now,
        )
        stripped = str(user_turn.payload["text"])

        diary_match = _DIARY_RE.fullmatch(stripped)
        explicit_diary = diary_match is not None
        entry_text = (diary_match.group(1) or "").strip() if diary_match else stripped
        morning_key = (
            self._outstanding_morning(profile_id, now)
            if _looks_like_morning_answer(stripped)
            else None
        )
        is_diary = explicit_diary or morning_key is not None
        if explicit_diary and not entry_text:
            return "После /сон добавьте текст записи — пустую запись я не сохраняю."

        if is_diary:
            payload: dict[str, Any] = {"text": entry_text}
            dream = _dream(entry_text)
            if dream:
                payload["dream"] = dream
            if morning_key is not None:
                payload["morning_prompt_key"] = morning_key
            self.store.put(
                profile_id, "sleep", "diary", source_key, payload, at=now
            )

        durable_is_diary = bool(
            self.store.list(profile_id, "sleep", "diary", limit=100)
            and any(
                row.source_key == source_key
                for row in self.store.list(profile_id, "sleep", "diary", limit=100)
            )
        )
        prompt = self._prompt_payload(profile_id, stripped, durable_is_diary)
        try:
            reply = self.brain(_SYSTEM_PROMPT, prompt)
            reply = _phone_length(reply.strip())
            if not reply:
                raise ValueError("empty brain response")
        except Exception:  # noqa: BLE001 - provider boundary must degrade safely
            reply = self._fallback(profile_id, is_diary)

        self.store.put(
            profile_id,
            "sleep",
            "turn",
            f"assistant:{source_key}",
            {"role": "assistant", "text": reply},
            at=now,
        )
        return reply

    def due(self, profile_id: UUID, now: datetime) -> list[Notice]:
        local = _aware(now).astimezone(_MOSCOW)
        notices: list[Notice] = []
        enabled, morning_at = self._schedule(profile_id)
        if enabled and morning_at <= local.time().replace(tzinfo=None) < time(12):
            key = f"morning:{local.date().isoformat()}"
            if not self._notice_delivered(profile_id, key):
                notices.append(
                    Notice(key, "Доброе утро. Как спалось и как вы себя чувствуете после пробуждения?")
                )

        if local.weekday() == 6 and local.time().replace(tzinfo=None) >= time(18):
            key = f"weekly:{local.date().isoformat()}"
            recent = self._recent_week_entries(profile_id, local)
            if len(recent) >= 3 and not self._notice_delivered(profile_id, key):
                notices.append(
                    Notice(key, "Пора коротко сверить неделю сна. Напишите /итоги, чтобы разобрать ваши записи.")
                )
        return notices

    def _set_schedule(
        self, profile_id: UUID, value: str | None, source_key: str, now: datetime
    ) -> str:
        previous = next(
            (
                row
                for row in self.store.list(profile_id, "sleep", "schedule", limit=100)
                if row.source_key == source_key
            ),
            None,
        )
        if previous is not None:
            return _schedule_confirmation(previous.payload)
        if value is None:
            enabled, morning_at = self._schedule(profile_id)
            return (
                f"Утренний вопрос: {morning_at.strftime('%H:%M')} по Москве."
                if enabled
                else "Утренние вопросы выключены."
            )
        if value.lower() == "выкл":
            payload = {"enabled": False, "time": "09:00", "timezone": "Europe/Moscow"}
            record = self.store.put(
                profile_id, "sleep", "schedule", source_key, payload, at=now
            )
            return _schedule_confirmation(record.payload)
        try:
            parsed = time.fromisoformat(value)
        except ValueError:
            return "Укажите время как /утро HH:MM или выключите: /утро выкл."
        if parsed.second or parsed.microsecond:
            return "Укажите время с точностью до минут, например /утро 09:00."
        if not time(6) <= parsed < time(12):
            return "Утренний вопрос можно назначить с 06:00 до 11:59 по Москве."
        payload = {
            "enabled": True,
            "time": parsed.strftime("%H:%M"),
            "timezone": "Europe/Moscow",
        }
        record = self.store.put(
            profile_id, "sleep", "schedule", source_key, payload, at=now
        )
        return _schedule_confirmation(record.payload)

    def _schedule(self, profile_id: UUID) -> tuple[bool, time]:
        rows = self.store.list(profile_id, "sleep", "schedule", limit=1)
        if not rows:
            return True, time(9)
        payload = rows[0].payload
        try:
            parsed = time.fromisoformat(str(payload.get("time", "09:00")))
        except ValueError:
            parsed = time(9)
        return bool(payload.get("enabled", True)), parsed

    def _render_diary(self, profile_id: UUID) -> str:
        entries = self.store.list(profile_id, "sleep", "diary", limit=14)
        if not entries:
            return "В дневнике пока нет записей. Добавьте первую: /сон как спалось."
        lines = ["Последние записи сна:"]
        for record in entries:
            date = record.at.astimezone(_MOSCOW).strftime("%d.%m")
            lines.append(f"{date} — {record.payload.get('text', '')}")
        return _phone_length("\n".join(lines))

    def _weekly_summary(self, profile_id: UUID, source_key: str, now: datetime) -> str:
        existing = self._turn(profile_id, f"assistant:{source_key}")
        if existing is not None:
            return str(existing.payload["text"])
        entries = self._recent_week_entries(profile_id, now.astimezone(_MOSCOW))
        if not entries:
            return "За последние 7 дней нет записей сна, поэтому честный итог пока невозможен."
        self.store.put(
            profile_id, "sleep", "turn", f"user:{source_key}",
            {"role": "user", "text": "/итоги"}, at=now,
        )
        payload = self._prompt_payload(profile_id, "/итоги", False)
        payload["task"] = "weekly_reflection"
        frame = _weekly_frame(entries, now.astimezone(_MOSCOW))
        payload["factual_frame"] = frame
        try:
            reflection = self.brain(_SYSTEM_PROMPT, payload).strip()
            if not reflection:
                raise ValueError("empty brain response")
            reply = _phone_length(f"{frame}\n{reflection}")
        except Exception:  # noqa: BLE001 - provider boundary must degrade safely
            reply = f"{frame}\nДля содержательного разбора нужен доступ к помощнику; сами записи сохранены."
        self.store.put(
            profile_id, "sleep", "turn", f"assistant:{source_key}",
            {"role": "assistant", "text": reply}, at=now,
        )
        return reply

    def _prompt_payload(self, profile_id: UUID, text: str, is_diary: bool) -> dict[str, Any]:
        turns = list(reversed(self.store.list(profile_id, "sleep", "turn", limit=12)))
        entries = list(reversed(self.store.list(profile_id, "sleep", "diary", limit=14)))
        goals = self.store.list(profile_id, "shared", "goal", limit=20)
        health: dict[str, Any] = {}
        if self.health_context is not None:
            try:
                health = self.health_context(profile_id, text)
            except Exception:  # noqa: BLE001 - optional integration is non-critical
                health = {}
        return {
            "request": text,
            "request_is_diary": is_diary,
            "conversation": [row.payload for row in turns],
            "diary_user_reports": [
                {"at": row.at.isoformat(), **row.payload} for row in entries
            ],
            "goals_not_evidence": [row.payload for row in goals],
            "verified_health_context": health,
        }

    def _outstanding_morning(self, profile_id: UUID, now: datetime) -> str | None:
        local_date = now.astimezone(_MOSCOW).date().isoformat()
        key = f"morning:{local_date}"
        notice = self._delivered_notice(profile_id, key)
        if notice is None:
            return None
        delivered_at = _delivery_at(notice)
        if now < delivered_at:
            return None
        for entry in self.store.list(profile_id, "sleep", "diary", limit=100):
            if entry.payload.get("morning_prompt_key") == key:
                return None
        return key

    def _notice_delivered(self, profile_id: UUID, key: str) -> bool:
        return self._delivered_notice(profile_id, key) is not None

    def _delivered_notice(self, profile_id: UUID, key: str) -> Record | None:
        return next(
            (
                row
                for row in self.store.list(profile_id, "sleep", "notice", limit=100)
                if row.source_key == key
            ),
            None,
        )

    def _recent_week_entries(self, profile_id: UUID, local_now: datetime) -> list[Record]:
        cutoff = local_now - timedelta(days=7)
        return [
            row
            for row in self.store.list(profile_id, "sleep", "diary", limit=100)
            if row.at.astimezone(_MOSCOW) >= cutoff
        ]

    def _turn(self, profile_id: UUID, source_key: str) -> Record | None:
        return next(
            (
                row
                for row in self.store.list(profile_id, "sleep", "turn", limit=100)
                if row.source_key == source_key
            ),
            None,
        )

    def _fallback(self, profile_id: UUID, saved_diary: bool) -> str:
        enabled, morning_at = self._schedule(profile_id)
        schedule = (
            f"Утренний вопрос настроен на {morning_at.strftime('%H:%M')} по Москве."
            if enabled
            else "Утренние вопросы сейчас выключены."
        )
        if saved_diary:
            return f"Запись сна сохранена. {schedule} Когда помощник снова будет доступен, продолжим без потери записи."
        return f"Сейчас не удалось получить содержательный ответ помощника. {schedule}"


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return value


def _dream(text: str) -> str | None:
    match = _DREAM_RE.search(text)
    return match.group(1) if match else None


def _looks_like_morning_answer(text: str) -> bool:
    """Conservatively distinguish a subjective check-in from a free question."""
    return not text.startswith("/") and _QUESTION_RE.search(text.strip()) is None


def _delivery_at(notice: Record) -> datetime:
    raw = notice.payload.get("delivery_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            return _aware(parsed)
        except ValueError:
            pass
    return _aware(notice.at)


def _weekly_frame(entries: list[Record], local_now: datetime) -> str:
    del local_now  # Entries have already been restricted to the requested rolling week.
    dates = [row.at.astimezone(_MOSCOW).date() for row in entries]
    start = min(dates).strftime("%d.%m")
    end = max(dates).strftime("%d.%m")
    count = len(entries)
    if count % 10 == 1 and count % 100 != 11:
        noun = "запись"
    elif count % 10 in {2, 3, 4} and count % 100 not in {12, 13, 14}:
        noun = "записи"
    else:
        noun = "записей"
    return f"За {start}–{end}: {count} {noun}."


def _schedule_confirmation(payload: dict[str, Any]) -> str:
    if not bool(payload.get("enabled", True)):
        return "Утренние вопросы выключены. Дневник и /итоги остаются доступны."
    scheduled = str(payload.get("time", "09:00"))
    return f"Буду задавать утренний вопрос в {scheduled} по Москве."


def _phone_length(text: str) -> str:
    if len(text) <= 1200:
        return text
    return text[:1199].rstrip() + "…"


_SYSTEM_PROMPT = """Ты ведёшь непрерывный дневник сна на русском языке.
Ответ должен быть удобен для телефона и не длиннее 1200 символов: вывод, одно релевантное
основание и следующий шаг или один полезный вопрос. Записи дневника — слова пользователя,
а не диагнозы. Не выдумывай причины, измерения или медицинские факты. Цели могут направлять
обратную связь, но не являются доказательством здоровья. Используй только релевантный
проверенный health context, не выгружай списки анализов и источников. Срочные риски обрабатывает
внешняя защитная граница Brain; не ослабляй её указания."""
