from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from health_agent.pilot.contracts import Record
from health_agent.pilot.training import TrainingCoach


class MemoryStore:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, Record]] = []

    def put(self, profile_id, domain, kind, source_key, payload, *, at=None):
        for owner, record in self.records:
            if owner == profile_id and (
                record.domain,
                record.kind,
                record.source_key,
            ) == (domain, kind, source_key):
                return record
        record = Record(
            f"r{len(self.records)}",
            domain,
            kind,
            source_key,
            at or datetime.now(UTC),
            payload,
        )
        self.records.append((profile_id, record))
        return record

    def list(self, profile_id, domain, kind=None, *, limit=100):
        found = [
            r
            for owner, r in self.records
            if owner == profile_id
            and r.domain == domain
            and (kind is None or r.kind == kind)
        ]
        return sorted(found, key=lambda r: r.at, reverse=True)[:limit]

    def get(self, profile_id, record_id):
        return next(
            (
                r
                for owner, r in self.records
                if owner == profile_id and r.id == record_id
            ),
            None,
        )

    def patch(self, profile_id, record_id, payload):
        for index, (owner, record) in enumerate(self.records):
            if owner == profile_id and record.id == record_id:
                updated = replace(record, payload=payload)
                self.records[index] = (owner, updated)
                return updated
        raise KeyError(record_id)


class Brain:
    def __init__(self) -> None:
        self.calls = []

    def __call__(self, system, payload, *, image_path=None):
        self.calls.append((system, payload))
        if payload.get("task") == "weekly_plan":
            return (
                "Черновик: две лёгкие тренировки и день отдыха. Что обычно тренируете?"
            )
        if payload.get("task") == "reflection":
            return "Факт: выполнена одна пробежка. Данных недостаточно, чтобы считать остальные занятия пропущенными."
        return "Помню контекст и отвечаю по тренировкам."


def test_goal_proposal_acceptance_activities_and_reflection_are_durable():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    store.put(
        profile,
        "shared",
        "goal",
        "annual",
        {
            "domain": "training",
            "text": "Бегать регулярно",
            "events": [{"name": "осенний старт", "date": "примерно осенью"}],
        },
        at=now,
    )
    activities = lambda *_: [
        {
            "id": "a1",
            "sport": "run",
            "started_at": "2026-09-06T08:00:00Z",
            "duration_s": 1800,
        }
    ]
    coach = TrainingCoach(store, brain, activity_source=activities)

    assert "Бегать регулярно" in coach.handle(profile, "/год", source_key="g1", now=now)
    proposal = coach.handle(profile, "/план", source_key="p1", now=now)
    assert proposal and not store.list(profile, "training", "accepted_plan")
    assert coach.handle(profile, "/сохранить план", source_key="p2", now=now)
    assert (
        store.list(profile, "training", "accepted_plan")[0].payload["text"] == proposal
    )
    summary = coach.handle(profile, "/итоги", source_key="p3", now=now)
    assert "пробежка" in summary
    assert store.list(profile, "training", "activity")

    restarted = TrainingCoach(store, brain, activity_source=activities)
    assert restarted.handle(
        profile, "/сохранить план", source_key="p2", now=now
    ) == coach.handle(profile, "/сохранить план", source_key="p2", now=now)
    assert len(store.list(profile, "training", "accepted_plan")) == 1


def test_profiles_are_isolated_and_missing_data_is_explicit():
    store, brain, first, second = MemoryStore(), Brain(), uuid4(), uuid4()
    now = datetime(2026, 9, 13, 19, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    coach.handle(first, "Болит колено после бега", source_key="m1", now=now)
    coach.handle(first, "/план", source_key="p1", now=now)
    coach.handle(first, "/сохранить план", source_key="a1", now=now)

    result = coach.handle(second, "/итоги", source_key="i2", now=now)
    assert "нет данных" in result.lower()
    assert "Болит" not in str(brain.calls[-1])
    notice = coach.due(first, now)
    assert len(notice) == 1 and "данн" in notice[0].text.lower()
    store.put(
        first, "shared", "notice", notice[0].key, {"text": notice[0].text}, at=now
    )
    assert coach.due(first, now) == []


def test_free_dialogue_replays_without_second_model_call_and_remembers_context():
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    coach = TrainingCoach(store, brain)
    first = coach.handle(profile, "Хочу снова начать бегать", source_key="m1", now=now)
    calls = len(brain.calls)
    assert (
        coach.handle(profile, "Хочу снова начать бегать", source_key="m1", now=now)
        == first
    )
    assert len(brain.calls) == calls
    coach.handle(
        profile, "Что мы решили?", source_key="m2", now=now + timedelta(minutes=1)
    )
    assert "Хочу снова начать бегать" in str(brain.calls[-1])
