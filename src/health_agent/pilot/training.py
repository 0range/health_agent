"""Annual goals, weekly planning, and factual training reflection."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from health_agent.pilot.contracts import (
    ActivitySource,
    Attachment,
    Brain,
    Notice,
    Record,
    Store,
)

_DOMAIN = "training"


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
        if now.weekday() != 6 or now.hour < 18:
            return []
        week = now.date().isocalendar()
        key = f"training-weekly-{week.year}-W{week.week:02d}"
        if self._by_source(profile_id, "notice", key, domain="shared") is not None:
            return []
        accepted = self._store.list(profile_id, _DOMAIN, "accepted_plan", limit=1)
        activities = self._import_activities(profile_id, now - timedelta(days=7), now)
        if not accepted and not activities:
            return []
        text = self._cached_reflection(profile_id, key, now, accepted, activities)
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
        activities = self._import_activities(profile_id, now - timedelta(days=28), now)
        goals = [
            r.payload
            for r in self._store.list(profile_id, "shared", "goal")
            if _training_goal(r)
        ]
        sparse = len(activities) < 2
        payload = {
            "task": "weekly_plan",
            "annual_goals": goals,
            "recent_activities": activities,
            "history_is_insufficient": sparse,
            "requirements": (
                "Write a conservative, high-level seven-day draft in Russian. Do not invent races, "
                "dates, pace, fitness, or completed work. If history is insufficient, avoid prescribed "
                "intensity and finish with exactly one useful question."
            ),
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except (RuntimeError, ValueError, TypeError):
            answer = "Черновик недели: чередуйте лёгкую активность и отдых без заданного темпа. Какой объём тренировок для вас привычен сейчас?"
        if sparse and (answer.count("?") != 1 or not answer.rstrip().endswith("?")):
            answer = (
                answer.replace("?", ".").rstrip()
                + "\nКакой объём тренировок для вас привычен сейчас?"
            )
        self._store.put(
            profile_id, _DOMAIN, "proposal", source_key, {"text": answer}, at=now
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
        except (RuntimeError, ValueError, TypeError):
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
        payload = {
            "task": "dialogue",
            "message": text,
            "conversation": self._conversation_payloads(profile_id),
            "requirements": "Reply in concise Russian; distinguish user reports, plans, and completed activity facts.",
        }
        try:
            answer = self._brain(_SYSTEM, payload)
        except (RuntimeError, ValueError, TypeError):
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

    def _conversation_payloads(self, profile_id: UUID) -> list[dict[str, Any]]:
        records = self._store.list(profile_id, _DOMAIN, "conversation", limit=12)
        return [r.payload for r in reversed(records)]

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
                    self._store.put(
                        profile_id,
                        _DOMAIN,
                        "activity",
                        f"activity:{stable}",
                        dict(activity),
                        at=_activity_at(activity, until),
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


def _activity_at(activity: dict[str, Any], fallback: datetime) -> datetime:
    value = (
        activity.get("started_at") or activity.get("start_time") or activity.get("date")
    )
    if isinstance(value, str):
        try:
            return _aware(datetime.fromisoformat(value))
        except ValueError:
            pass
    return fallback


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


_SYSTEM = (
    "You are a conservative training planning assistant. Use only supplied goals, "
    "user reports, accepted plans, and factual activities. Never invent fitness, "
    "race registration, exact event dates, pace goals, symptoms, or completion. "
    "Never diagnose, prescribe clinical treatment, or set medical targets. Treat "
    "symptoms only as user reports and direct urgent concerns to appropriate care."
)
