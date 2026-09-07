from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from health_agent.pilot.contracts import Attachment, Record
from health_agent.pilot.food import FoodCoach


class MemoryStore:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, Record]] = []

    def put(self, profile_id: UUID, domain: str, kind: str, source_key: str,
            payload: dict[str, Any], *, at: datetime | None = None) -> Record:
        for owner, record in self.records:
            if owner == profile_id and (record.domain, record.kind, record.source_key) == (domain, kind, source_key):
                return record
        record = Record(str(len(self.records) + 1), domain, kind, source_key,
                        at or datetime.now(UTC), payload)
        self.records.append((profile_id, record))
        return record

    def list(self, profile_id: UUID, domain: str, kind: str | None = None,
             *, limit: int = 100) -> list[Record]:
        values = [r for owner, r in self.records if owner == profile_id and r.domain == domain
                  and (kind is None or r.kind == kind)]
        return sorted(values, key=lambda r: r.at, reverse=True)[:limit]

    def get(self, profile_id: UUID, record_id: str) -> Record | None:
        return next((r for owner, r in self.records if owner == profile_id and r.id == record_id), None)

    def patch(self, profile_id: UUID, record_id: str, payload: dict[str, Any]) -> Record:
        for index, (owner, record) in enumerate(self.records):
            if owner == profile_id and record.id == record_id:
                updated = replace(record, payload=payload)
                self.records[index] = (owner, updated)
                return updated
        raise KeyError(record_id)


class Brain:
    def __init__(self, reply: str | Exception | None = None) -> None:
        self.reply = reply or json.dumps({
            "foods": ["овощи", "рис"], "portion_estimate": "примерно одна тарелка",
            "kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None,
            "saturated_fat_g": None, "fiber_g": None, "cholesterol_mg": None,
            "confidence": 0.5, "unknowns": ["масса продуктов"],
            "feedback": "В тарелке есть овощи; при желании добавьте источник белка.",
        }, ensure_ascii=False)
        self.calls: list[tuple[dict[str, Any], Path | None]] = []

    def __call__(self, system: str, payload: dict[str, Any], *,
                 image_path: Path | None = None) -> str:
        self.calls.append((payload, image_path))
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


@pytest.fixture
def setup() -> tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]:
    store, brain = MemoryStore(), Brain()
    return store, brain, FoodCoach(store, brain), uuid4(), datetime(2026, 9, 7, 12, tzinfo=UTC)


def test_meal_due_correction_and_delivery_suppression(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, _, coach, profile, noon = setup
    coach.handle(profile, "/ел 12:00 Обед: рис и овощи", source_key="meal-1", now=noon)
    assert coach.due(profile, noon + timedelta(hours=3)) == []
    notices = coach.due(profile, noon + timedelta(hours=3, minutes=30))
    assert len(notices) == 1
    old_key = notices[0].key

    coach.handle(profile, "/время 13:00", source_key="correction", now=noon + timedelta(hours=1))
    assert coach.due(profile, noon + timedelta(hours=3, minutes=30)) == []
    notice = coach.due(profile, noon + timedelta(hours=4, minutes=30))[0]
    assert notice.key != old_key
    store.put(profile, "food", "notice", notice.key, {"delivered": True}, at=noon + timedelta(hours=4, minutes=30))
    assert coach.due(profile, noon + timedelta(hours=5)) == []


def test_photo_replay_is_idempotent_and_persists_before_failed_analysis(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(RuntimeError("offline")), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 12, tzinfo=UTC)
    photo = Attachment(tmp_path / "meal.jpg", "image/jpeg", "обед")
    assert "сохран" in coach.handle(profile, "", source_key="photo-1", now=now, attachment=photo).lower()
    assert len(store.list(profile, "food", "meal")) == 1
    assert coach.due(profile, now + timedelta(hours=3, minutes=30))

    brain.reply = Brain().reply
    coach.handle(profile, "", source_key="photo-1", now=now, attachment=photo)
    meals = store.list(profile, "food", "meal")
    assert len(meals) == 1
    assert meals[0].payload["analysis"] is not None
    assert brain.calls[-1][1] == photo.path


def test_dinner_quiet_hours_skip_pause_and_restart(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, brain, coach, profile, noon = setup
    late = noon.replace(hour=21)
    coach.handle(profile, "/ел 21:00 Ужин", source_key="dinner", now=late)
    assert coach.due(profile, late + timedelta(hours=10)) == []

    lunch = noon + timedelta(days=1)
    coach.handle(profile, "/ел 12:00 Обед", source_key="lunch", now=lunch)
    coach.handle(profile, "/позже 30", source_key="snooze", now=lunch + timedelta(hours=3, minutes=30))
    assert coach.due(profile, lunch + timedelta(hours=3, minutes=45)) == []
    assert FoodCoach(store, brain).due(profile, lunch + timedelta(hours=4))
    coach.handle(profile, "/пропустить", source_key="skip", now=lunch + timedelta(hours=4))
    assert coach.due(profile, lunch + timedelta(hours=5)) == []

    coach.handle(profile, "/напоминания выкл", source_key="off", now=noon)
    assert coach.due(profile, noon + timedelta(hours=6)) == []
    coach.handle(profile, "/напоминания вкл", source_key="on", now=noon + timedelta(hours=1))
    assert "включ" in coach.handle(profile, "/напоминания вкл", source_key="on", now=noon + timedelta(hours=1)).lower()


def test_unknown_nutrients_stay_null_and_bad_precision_is_rejected(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, brain, coach, profile, noon = setup
    brain.reply = '{"foods":["суп"],"portion_estimate":"миска","kcal":412.37,"protein_g":null,"fat_g":null,"carbs_g":null,"saturated_fat_g":null,"fiber_g":null,"cholesterol_mg":null,"confidence":0.4,"unknowns":[],"feedback":"В тарелке суп."}'
    coach.handle(profile, "суп", source_key="soup", now=noon)
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["kcal"] is None
    assert analysis["protein_g"] is None
    assert "неизвест" in coach.handle(profile, "/сегодня", source_key="today", now=noon).lower()


def test_naive_and_implausible_times_request_clarification(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    _, _, coach, profile, noon = setup
    with pytest.raises(ValueError, match="timezone-aware"):
        coach.handle(profile, "обед", source_key="bad", now=noon.replace(tzinfo=None))
    assert "уточ" in coach.handle(profile, "/ел 23:00 ужин", source_key="future", now=noon).lower()
