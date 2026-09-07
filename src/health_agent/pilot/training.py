"""Annual goals, weekly planning, and factual training reflection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from health_agent.pilot.contracts import (
    ActivitySource,
    Attachment,
    Brain,
    Notice,
    Record,
    Store,
)
from health_agent.pilot.coros_sync import normalize_coros_activity

_DOMAIN = "training"
_MOSCOW = ZoneInfo("Europe/Moscow")


class TrainingCoach:
    def __init__(
        self,
        store: Store,
        brain: Brain,
        *,
        activity_source: ActivitySource | None = None,
    ) -> None:
        self._store = store
        self._brain = brain
        self._activity_source = activity_source

    def handle(
        self,
        profile_id: UUID,
        text: str,
        *,
        source_key: str,
        now: datetime,
        attachment: Attachment | None = None,
    ) -> str:
        del attachment
        now = _aware(now)
        replay = self._by_source(profile_id, "reply", source_key)
        if replay is not None:
            return str(replay.payload["text"])

        command = text.strip().lower()
        if command == "/год":
            answer = self._annual_goals(profile_id)
        elif command == "/план":
            answer = self._propose(profile_id, source_key, now)
        elif command == "/сохранить план":
            answer = self._accept(profile_id, source_key, now)
        elif command == "/итоги":
            answer = self._reflection(profile_id, source_key, now, on_demand=True)
        else:
            answer = self._dialogue(profile_id, text, source_key, now)
        self._store.put(
            profile_id, _DOMAIN, "reply", source_key, {"text": answer}, at=now
        )
        return answer

    def due(self, profile_id: UUID, now: datetime) -> list[Notice]:
        now = _aware(now)
        local_now = now.astimezone(_MOSCOW)
        if local_now.weekday() != 6 or local_now.hour < 18:
            return []
        week = local_now.date().isocalendar()
        key = f"training-weekly-{week.year}-W{week.week:02d}"
        if self._by_source(profile_id, "notice", key) is not None:
            return []
        preferences = self._weekly_preferences(profile_id)
        goals = [
            r.payload
            for r in self._store.list(profile_id, "shared", "goal")
            if _training_goal(r)
        ]
        accepted = self._store.list(profile_id, _DOMAIN, "accepted_plan", limit=1)
        activities = self._import_activities(profile_id, now - timedelta(days=7), now)
        if not preferences and not goals and not accepted and not activities:
            return []
        draft = self._propose(profile_id, _draft_key(local_now), now)
        label = "Ориентир на следующую неделю, не обязательство\n"
        reflection = ""
        if accepted or activities:
            reflection = self._cached_reflection(profile_id, key, now, accepted, activities)[:750]
        prefix = reflection + "\n\n" if reflection else ""
        text = prefix + label + draft[:1800 - len(prefix) - len(label)]
        return [Notice(key, text)]

    def _annual_goals(self, profile_id: UUID) -> str:
        goals = [
            r
            for r in self._store.list(profile_id, "shared", "goal")
            if _training_goal(r)
        ]
        if not goals:
            return "Годовые цели по тренировкам пока не сохранены. Добавьте цель и примерный период события."
        lines = ["Цели на год:"]
        for goal in reversed(goals):
            text = (
                goal.payload.get("text")
                or goal.payload.get("title")
                or goal.payload.get("goal")
            )
            if text:
                lines.append(f"• {text}")
            events = goal.payload.get("events", [])
            if isinstance(events, list):
                for event in events:
                    if isinstance(event, dict):
                        name = event.get("name") or event.get("title")
                        when = (
                            event.get("approximate_date")
                            or event.get("period")
                            or event.get("date")
                        )
                        if name:
                            lines.append(
                                f"  — {name}"
                                + (f", {when}" if when else ", дата не уточнена")
                            )
        return "\n".join(lines)

    def _propose(self, profile_id: UUID, source_key: str, now: datetime) -> str:
        cached = self._by_source(profile_id, "proposal", source_key)
        if cached is not None:
            return str(cached.payload["text"])
        activities = self._import_activities(profile_id, now - timedelta(days=28), now)
        goals = [
            r.payload
            for r in self._store.list(profile_id, "shared", "goal")
            if _training_goal(r)
        ]
        sparse = len(activities) < 2
        preferences = self._weekly_preferences(profile_id)
        start, end = _next_week(now)
        payload = {
            "task": "weekly_plan",
            "annual_goals": goals,
            "weekly_preferences": preferences,
            "recent_activities": activities,
            "recent_training_messages": self._conversation_payloads(profile_id),
            "next_week": {"monday": start.isoformat(), "sunday": end.isoformat()},
            "history_is_insufficient": sparse,
            "requirements": (
                "Write an orienting, adjustable seven-day draft in Russian for the supplied exact "
                "dates. Adjust it to reported recovery and availability. Do not invent races, pace, "
                "fitness, completed work, working weights, or required intensity. Keep beginner "
                "strength conservative. If history is insufficient, avoid prescribed intensity and "
                "finish with exactly one useful question."
            ),
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except Exception:  # noqa: BLE001 -- provider boundary; storage stays outside
            answer = _fallback_draft(preferences, start, end)
        if sparse and (answer.count("?") != 1 or not answer.rstrip().endswith("?")):
            answer = (
                answer.replace("?", ".").rstrip()
                + "\nКакой объём тренировок для вас привычен сейчас?"
            )
        self._store.put(
            profile_id,
            _DOMAIN,
            "proposal",
            source_key,
            {
                "text": answer,
                "preferences": preferences,
                "date_range": {"monday": start.isoformat(), "sunday": end.isoformat()},
            },
            at=now,
        )
        return answer

    def _accept(self, profile_id: UUID, source_key: str, now: datetime) -> str:
        proposals = self._store.list(profile_id, _DOMAIN, "proposal", limit=1)
        if not proposals:
            return "Сначала создайте черновик командой /план. Ничего не сохранено как принятый план."
        proposal = proposals[0]
        self._store.put(
            profile_id,
            _DOMAIN,
            "accepted_plan",
            source_key,
            {"proposal_id": proposal.id, "text": proposal.payload["text"]},
            at=now,
        )
        return "План принят и сохранён. Его можно использовать для следующего разбора недели."

    def _reflection(
        self, profile_id: UUID, source_key: str, now: datetime, *, on_demand: bool
    ) -> str:
        accepted = self._store.list(profile_id, _DOMAIN, "accepted_plan", limit=1)
        activities = self._import_activities(profile_id, now - timedelta(days=7), now)
        if not accepted and not activities:
            return "Для итогов пока нет данных: нет принятого плана и фактических тренировок. Я не буду считать занятия пропущенными."
        return self._generate_reflection(
            profile_id, source_key, now, accepted, activities, on_demand
        )

    def _cached_reflection(
        self,
        profile_id: UUID,
        key: str,
        now: datetime,
        accepted: list[Record],
        activities: list[dict[str, Any]],
    ) -> str:
        cached = self._by_source(profile_id, "reflection", key)
        if cached:
            return str(cached.payload["text"])
        return self._generate_reflection(
            profile_id, key, now, accepted, activities, False
        )

    def _generate_reflection(
        self,
        profile_id: UUID,
        key: str,
        now: datetime,
        accepted: list[Record],
        activities: list[dict[str, Any]],
        on_demand: bool,
    ) -> str:
        conversations = self._conversation_payloads(profile_id)
        payload = {
            "task": "reflection",
            "accepted_plan": accepted[0].payload if accepted else None,
            "actual_activities": activities,
            "recent_training_messages": conversations,
            "data_missing": not activities,
            "requirements": "Compare only known facts. Mention missing activity data and never label an unknown workout as missed. Note user-reported symptoms without diagnosing.",
            "on_demand": on_demand,
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except Exception:  # noqa: BLE001 -- provider boundary; storage stays outside
            answer = (
                "Итог ограничен сохранёнными фактами. Данных об активностях нет; это не означает, что тренировки были пропущены."
                if not activities
                else f"Сохранено фактических активностей: {len(activities)}. Сверьте самочувствие с принятым планом."
            )
        if not activities and "данн" not in answer.lower():
            answer += "\nДанных о фактических тренировках нет; это не означает пропуск."
        self._store.put(
            profile_id, _DOMAIN, "reflection", key, {"text": answer}, at=now
        )
        return answer

    def _dialogue(
        self, profile_id: UUID, text: str, source_key: str, now: datetime
    ) -> str:
        self._store.put(
            profile_id,
            _DOMAIN,
            "conversation",
            f"user:{source_key}",
            {"role": "user", "text": text},
            at=now,
        )
        normalized = text.strip().lower()
        if normalized in {"покажи план", "покажи черновик", "покажи план на неделю"}:
            latest = self._latest_payload(profile_id, "proposal")
            return (
                str(latest["text"])
                if latest
                else "Черновика пока нет. Скажите: «давай план на неделю»."
            )
        if "давай план на неделю" in normalized:
            return self._propose(profile_id, source_key, now)
        if _is_revision_request(normalized, self._latest_payload(profile_id, "proposal")):
            return self._revise(profile_id, text, source_key, now)
        payload = {
            "task": "dialogue",
            "message": text,
            "conversation": self._conversation_payloads(profile_id),
            "latest_proposal": self._latest_payload(profile_id, "proposal"),
            "latest_accepted_plan": self._latest_payload(profile_id, "accepted_plan"),
            "requirements": (
                "Reply in concise Russian; distinguish user reports, plans, and completed activity "
                "facts. Acknowledge availability constraints as reported constraints, not accepted "
                "schedule changes."
            ),
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except Exception:  # noqa: BLE001 -- provider boundary; storage stays outside
            answer = "Сообщение о тренировках сохранено. Анализ сейчас недоступен."
        self._store.put(
            profile_id,
            _DOMAIN,
            "conversation",
            f"assistant:{source_key}",
            {"role": "assistant", "text": answer},
            at=now,
        )
        return answer

    def _revise(
        self, profile_id: UUID, text: str, source_key: str, now: datetime
    ) -> str:
        current = self._latest_payload(profile_id, "proposal")
        if current is None:
            return self._propose(profile_id, source_key, now)
        preferences = self._weekly_preferences(profile_id)
        dates = _proposal_dates(current)
        if dates is None:
            return "У черновика нет корректных дат недели. Создайте новый черновик командой /план."
        start, end = dates
        payload = {
            "task": "revise_weekly_plan",
            "request": text,
            "current_proposal": current,
            "latest_proposal": current,
            "latest_accepted_plan": self._latest_payload(profile_id, "accepted_plan"),
            "conversation": self._conversation_payloads(profile_id),
            "weekly_preferences": preferences,
            "next_week": {"monday": start.isoformat(), "sunday": end.isoformat()},
            "requirements": (
                "Revise the draft in concise Russian while preserving explicit dates and known "
                "constraints. Do not accept the plan or mutate an accepted plan."
            ),
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except Exception:  # noqa: BLE001 -- provider boundary; storage stays outside
            answer = str(current["text"])
        self._store.put(
            profile_id,
            _DOMAIN,
            "proposal",
            source_key,
            {
                "text": answer,
                "preferences": preferences,
                "date_range": {"monday": start.isoformat(), "sunday": end.isoformat()},
                "revises": current,
            },
            at=now,
        )
        return answer

    def _weekly_preferences(self, profile_id: UUID) -> dict[str, Any] | None:
        record = self._by_source(
            profile_id, "settings", "weekly-preferences", domain=_DOMAIN
        )
        if record is None or record.payload.get("source") != "user":
            return None
        sessions = record.payload.get("sessions")
        if not isinstance(sessions, list):
            return None
        return record.payload

    def _conversation_payloads(self, profile_id: UUID) -> list[dict[str, Any]]:
        records = self._store.list(profile_id, _DOMAIN, "conversation", limit=12)
        return [r.payload for r in reversed(records)]

    def _latest_payload(self, profile_id: UUID, kind: str) -> dict[str, Any] | None:
        records = self._store.list(profile_id, _DOMAIN, kind, limit=1)
        return records[0].payload if records else None

    def _import_activities(
        self, profile_id: UUID, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        if self._activity_source is not None:
            try:
                for activity in self._activity_source(profile_id, since, until):
                    stable = (
                        activity.get("id")
                        or activity.get("activity_id")
                        or hashlib.sha256(
                            json.dumps(activity, sort_keys=True, default=str).encode()
                        ).hexdigest()[:24]
                    )
                    normalized, occurred_at = normalize_coros_activity(dict(activity))
                    self._store.put(
                        profile_id,
                        _DOMAIN,
                        "activity",
                        f"activity:{stable}",
                        normalized,
                        at=occurred_at,
                    )
            except (RuntimeError, ValueError, TypeError):
                # A read failure means unknown activity data, never a missed workout.
                return [
                    r.payload
                    for r in reversed(self._store.list(profile_id, _DOMAIN, "activity"))
                    if since <= _aware(r.at) <= until
                ]
        return [
            r.payload
            for r in reversed(self._store.list(profile_id, _DOMAIN, "activity"))
            if since <= _aware(r.at) <= until
        ]

    def _by_source(
        self, profile_id: UUID, kind: str, source_key: str, *, domain: str = _DOMAIN
    ) -> Record | None:
        return next(
            (
                r
                for r in self._store.list(profile_id, domain, kind)
                if r.source_key == source_key
            ),
            None,
        )


def _training_goal(record: Record) -> bool:
    domain = record.payload.get("domain")
    hierarchy = record.payload.get("hierarchy")
    return domain == "training" or hierarchy in ("training", ["training"])


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _next_week(now: datetime) -> tuple[date, date]:
    local_date = _aware(now).astimezone(_MOSCOW).date()
    monday = local_date + timedelta(days=7 - local_date.weekday())
    return monday, monday + timedelta(days=6)


def _draft_key(local_now: datetime) -> str:
    monday, _ = _next_week(local_now)
    week = monday.isocalendar()
    return f"training-draft-{week.year}-W{week.week:02d}"


def _proposal_dates(proposal: dict[str, Any]) -> tuple[date, date] | None:
    dates = proposal.get("date_range")
    if not isinstance(dates, dict):
        return None
    try:
        monday = date.fromisoformat(dates["monday"])
        sunday = date.fromisoformat(dates["sunday"])
    except (KeyError, ValueError, TypeError):
        return None
    if monday.weekday() != 0 or sunday - monday != timedelta(days=6):
        return None
    return monday, sunday


def _is_revision_request(text: str, current: dict[str, Any] | None = None) -> bool:
    if "?" in text or text.startswith(("почему", "можно", "как ", "зачем")):
        return False
    change = any(word in text for word in ("перенеси", "замени", "поменяй", "скорректируй"))
    target = any(word in text for word in ("план", "заняти", "трениров", "сесси"))
    if current:
        disciplines = ["бассейн", "плаван", "бег", "пробеж", "велосипед", "силов", "йог", "ходьб", "лыж"]
        preferences = current.get("preferences")
        if isinstance(preferences, dict):
            disciplines.extend(
                item["discipline"].lower()
                for item in preferences.get("sessions", [])
                if isinstance(item, dict) and isinstance(item.get("discipline"), str)
                and item["discipline"].strip()
            )
        target = target or any(word in text for word in disciplines)
    return change and target


def _fallback_draft(
    preferences: dict[str, Any] | None, start: date, end: date
) -> str:
    slots: list[str] = []
    if preferences:
        for item in preferences.get("sessions", []):
            if isinstance(item, dict) and isinstance(item.get("count"), int):
                slots.append(f"{item.get('discipline', 'тренировка')}: {item['count']} желаемых слота")
    desired = "; ".join(slots) if slots else "лёгкая активность и отдых по самочувствию"
    return (
        f"Черновик на {start.isoformat()}—{end.isoformat()}: {desired}. "
        "Распределите по доступности и восстановлению, без заданного темпа или веса. "
        "Какой объём тренировок для вас привычен сейчас?"
    )


_SYSTEM = (
    "You are a conservative training planning assistant. Use only supplied goals, "
    "user reports, accepted plans, and factual activities. Never invent fitness, "
    "race registration, exact event dates, pace goals, symptoms, or completion. "
    "Never diagnose, prescribe clinical treatment, or set medical targets. Treat "
    "symptoms only as user reports and direct urgent concerns to appropriate care."
)
