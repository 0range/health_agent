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
    return store, brain, FoodCoach(store, brain), uuid4(), datetime(2026, 9, 7, 9, tzinfo=UTC)


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
    assert coach.due(profile, now + timedelta(hours=3, minutes=50))

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


def test_utc_input_uses_moscow_wall_time_and_local_date(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, _, coach, profile, _ = setup
    # 21:30 UTC is already the next local calendar day in Moscow.
    now = datetime(2026, 9, 7, 21, 30, tzinfo=UTC)
    coach.handle(profile, "/ел 00:15 завтрак", source_key="midnight", now=now)
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["occurred_at"] == "2026-09-07T21:15:00+00:00"
    assert coach.due(profile, datetime(2026, 9, 8, 1, 0, tzinfo=UTC)) == []  # 04:00 Moscow

    reply = coach.handle(profile, "/время 00:20", source_key="local-correction", now=now)
    assert "00:20" in reply
    assert store.list(profile, "food", "meal")[0].payload["occurred_at"] == "2026-09-07T21:20:00+00:00"


def test_reminder_expires_and_bad_interval_config_falls_back(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, _, coach, profile, _ = setup
    noon_moscow = datetime(2026, 9, 7, 9, tzinfo=UTC)
    store.put(profile, "food", "settings", "protocol", {"interval_hours": 99})
    coach.handle(profile, "/ел 12:00 обед", source_key="meal", now=noon_moscow)
    assert coach.due(profile, noon_moscow + timedelta(hours=3, minutes=30))
    assert coach.due(profile, noon_moscow + timedelta(hours=4, minutes=31)) == []

    evening = datetime(2026, 9, 8, 18, tzinfo=UTC)  # 21:00 Moscow
    coach.handle(profile, "/ел 21:00 обед", source_key="late-labelled-lunch", now=evening)
    assert coach.due(profile, datetime(2026, 9, 9, 4, tzinfo=UTC)) == []  # no stale 07:00 alert


def test_snooze_rejects_expired_and_quiet_hour_targets_without_promising_delivery(
    setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime],
) -> None:
    store, _, coach, profile, noon = setup
    coach.handle(profile, "/ел 12:00 обед", source_key="meal", now=noon)
    late = coach.handle(
        profile, "/позже 30", source_key="late", now=noon + timedelta(hours=4, minutes=20)
    )
    assert "не могу" in late.lower()
    assert not store.list(profile, "food", "control")

    evening = datetime(2026, 9, 8, 18, 30, tzinfo=UTC)  # 21:30 Moscow
    coach.handle(profile, "/ел 21:30 обед", source_key="evening", now=evening)
    quiet = coach.handle(
        profile, "/позже 30", source_key="quiet", now=evening
    )
    assert "тихие часы" in quiet.lower()
    assert not store.list(profile, "food", "control")


def test_replayed_old_correction_does_not_reanchor_newer_meal(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, _, coach, profile, _ = setup
    first = datetime(2026, 9, 7, 9, tzinfo=UTC)
    coach.handle(profile, "/ел 12:00 обед", source_key="first", now=first)
    coach.handle(profile, "/время 12:10", source_key="correct-first", now=first + timedelta(minutes=10))
    second = first + timedelta(hours=3)
    coach.handle(profile, "/ел 15:00 полдник", source_key="second", now=second)
    second_before = store.list(profile, "food", "meal")[0].payload["occurred_at"]
    coach.handle(profile, "/время 12:10", source_key="correct-first", now=second)
    assert store.list(profile, "food", "meal")[0].payload["occurred_at"] == second_before


@pytest.mark.parametrize("unsafe", [
    "У вас диабет. Срочно прекратите лекарства.",
    "У вас рак.",
    "Молочные продукты запрещены.",
    "Этот продукт всем всегда запрещён.",
    "Овощей 53.27 грамма; обязательно голодайте сутки.",
    "Очень длинный совет. " * 50,
])
def test_unsafe_or_overprecise_feedback_gets_bounded_fallback(
    setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime], unsafe: str,
) -> None:
    store, brain, coach, profile, noon = setup
    value = json.loads(str(Brain().reply))
    value["feedback"] = unsafe
    brain.reply = json.dumps(value, ensure_ascii=False)
    answer = coach.handle(profile, "обед", source_key="unsafe", now=noon)
    assert len(answer) <= 280
    assert unsafe not in answer
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["feedback"] == answer
    assert analysis["feedback_untrusted"] == unsafe


def test_all_structured_fields_are_normalized_without_crash(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    store, brain, coach, profile, noon = setup
    brain.reply = json.dumps({
        "foods": "рис", "portion_estimate": ["тарелка"], "kcal": -1,
        "protein_g": float("inf"), "unknowns": "ничего", "confidence": "high",
        "feedback": {"advice": "eat"},
    })
    coach.handle(profile, "рис", source_key="malformed", now=noon)
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["foods"] == []
    assert analysis["portion_estimate"] is None
    assert isinstance(analysis["unknowns"], list)
    assert analysis["kcal"] is None and analysis["protein_g"] is None
    assert analysis["confidence"] is None


def test_summary_reports_actual_interval_adherence(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    _, _, coach, profile, _ = setup
    morning = datetime(2026, 9, 7, 6, tzinfo=UTC)
    coach.handle(profile, "/ел 09:00 завтрак", source_key="breakfast", now=morning)
    coach.handle(profile, "/ел 12:30 обед", source_key="lunch", now=morning + timedelta(hours=3, minutes=30))
    summary = coach.handle(profile, "/сегодня", source_key="summary", now=morning + timedelta(hours=4))
    assert "1 из 1" in summary


def test_profiles_are_isolated(setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime]) -> None:
    _, _, coach, profile, noon = setup
    other = uuid4()
    coach.handle(profile, "/ел 12:00 обед", source_key="same-source", now=noon)
    assert coach.due(other, noon + timedelta(hours=3, minutes=30)) == []
    assert "нет" in coach.handle(other, "/сегодня", source_key="other-summary", now=noon).lower()


def test_feedback_uses_controlled_plate_observation_and_protocol_change(
    setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime],
) -> None:
    store, brain, coach, profile, noon = setup
    store.put(profile, "food", "settings", "protocol", {
        "plate_rules": {"lunch": ["vegetables", "protein"]},
    })
    value = json.loads(str(Brain().reply))
    value["foods"] = ["салат"]
    value["feedback"] = "Любой произвольный текст модели."
    brain.reply = json.dumps(value, ensure_ascii=False)
    answer = coach.handle(profile, "/ел 12:00 обед", source_key="controlled", now=noon)
    assert answer == "В записи отмечены: овощи. По выбранному правилу можно добавить источник белка."
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["feedback_untrusted"] == "Любой произвольный текст модели."


def test_week_summary_excludes_overnight_and_post_dinner_adjacency(
    setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime],
) -> None:
    _, _, coach, profile, _ = setup
    dinner = datetime(2026, 9, 7, 18, tzinfo=UTC)  # 21:00 Moscow
    coach.handle(profile, "/ел 21:00 ужин", source_key="dinner-day-1", now=dinner)
    after_dinner = dinner + timedelta(minutes=30)
    coach.handle(profile, "/ел 21:30 перекус", source_key="after-dinner", now=after_dinner)
    coach.handle(
        profile, "/ел 22:00 перекус", source_key="second-after-dinner",
        now=dinner + timedelta(hours=1),
    )
    breakfast = datetime(2026, 9, 8, 6, tzinfo=UTC)
    coach.handle(profile, "/ел 09:00 завтрак", source_key="breakfast-day-2", now=breakfast)
    lunch = breakfast + timedelta(hours=3, minutes=30)
    coach.handle(profile, "/ел 12:30 обед", source_key="lunch-day-2", now=lunch)

    summary = coach.handle(profile, "/неделя", source_key="week", now=lunch)
    assert "1 из 1" in summary


def test_live_vision_component_dict_is_normalized_without_false_fruit_change(
    setup: tuple[MemoryStore, Brain, FoodCoach, UUID, datetime],
) -> None:
    store, brain, coach, profile, noon = setup
    store.put(profile, "food", "settings", "protocol", {
        "plate_rules": {"lunch": ["grains", "fruit"]},
    })
    brain.reply = json.dumps({
        "foods": ["Овсяная каша", "Малина", "Орехи (миндаль)"],
        "plate_components": {
            "grains": "Овсяная каша", "fruit": "Малина", "vegetables": None,
            "protein": None, "dairy": None,
        },
        "portion_estimate": None,
        "kcal": None, "protein_g": None, "fat_g": None, "carbs_g": None,
        "saturated_fat_g": None, "fiber_g": None, "cholesterol_mg": None,
        "confidence": 0.6, "unknowns": ["размер порции"],
        "feedback": "Молочные продукты запрещены.",
    }, ensure_ascii=False)
    answer = coach.handle(profile, "овсянка", source_key="live-photo", now=noon)
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["plate_components"] == ["grains", "fruit"]
    assert "добавить фрукты" not in answer.lower()
    assert "неизвест" in answer.lower()


def test_portion_correction_reanalyses_same_meal_and_photo_idempotently(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 7, 9, tzinfo=UTC)
    photo = Attachment(tmp_path / "oatmeal.jpg", "image/jpeg", "завтрак")
    brain.reply = json.dumps({
        "foods": ["Овсяная каша"], "plate_components": {"grains": "каша"},
        "portion_estimate": None, "kcal": None, "protein_g": None, "fat_g": None,
        "carbs_g": None, "saturated_fat_g": None, "fiber_g": None,
        "cholesterol_mg": None, "confidence": 0.5, "unknowns": ["порция"],
        "feedback": "ignored",
    }, ensure_ascii=False)
    coach.handle(profile, "", source_key="photo-meal", now=now, attachment=photo)
    meal_before = store.list(profile, "food", "meal")[0]
    due_before = coach.due(profile, now + timedelta(hours=3, minutes=50))[0].key

    brain.reply = Brain().reply
    answer = coach.handle(
        profile, "/порция 200 г", source_key="portion-fix", now=now + timedelta(minutes=5),
    )
    meal_after = store.list(profile, "food", "meal")[0]
    assert meal_after.id == meal_before.id
    assert meal_after.payload["occurred_at"] == meal_before.payload["occurred_at"]
    assert meal_after.payload["user_portion"] == "200 г"
    assert brain.calls[-1][0]["meal"]["portion"] == "200 г"
    assert brain.calls[-1][1] == photo.path
    assert coach.due(profile, now + timedelta(hours=3, minutes=50))[0].key == due_before
    calls = len(brain.calls)
    assert coach.handle(
        profile, "/порция 200 г", source_key="portion-fix", now=now + timedelta(minutes=5),
    ) == answer
    assert len(brain.calls) == calls
    assert len(store.list(profile, "food", "meal")) == 1


def test_failed_portion_reanalysis_retries_bound_old_meal_after_new_meal(tmp_path: Path) -> None:
    store, brain, profile = MemoryStore(), Brain(), uuid4()
    coach = FoodCoach(store, brain)
    first_at = datetime(2026, 9, 7, 6, tzinfo=UTC)
    photo = Attachment(tmp_path / "breakfast.jpg", "image/jpeg", "завтрак")
    coach.handle(profile, "", source_key="first-meal", now=first_at, attachment=photo)
    first = next(r for r in store.list(profile, "food", "meal") if r.source_key == "first-meal")
    previous_analysis = first.payload["analysis"]
    previous_raw = first.payload["analysis_raw"]

    brain.reply = RuntimeError("vision offline")
    failure = coach.handle(
        profile, "/порция 200 г", source_key="portion-retry", now=first_at + timedelta(minutes=5),
    )
    failed = store.get(profile, first.id)
    assert failed is not None
    assert "недоступ" in failure.lower()
    assert failed.payload["analysis"] is None
    assert failed.payload["analysis_status"] == "incomplete"
    assert failed.payload["previous_analysis"] == previous_analysis
    assert failed.payload["previous_analysis_raw"] == previous_raw

    second_at = first_at + timedelta(hours=4)
    brain.reply = Brain().reply
    coach.handle(profile, "/ел 13:00 обед", source_key="newer-meal", now=second_at)
    newer = next(r for r in store.list(profile, "food", "meal") if r.source_key == "newer-meal")
    newer_before = dict(newer.payload)
    calls_before_retry = len(brain.calls)

    answer = coach.handle(
        profile, "/порция 200 г", source_key="portion-retry", now=second_at,
    )
    retried = store.get(profile, first.id)
    assert retried is not None
    assert retried.payload["analysis"] is not None
    assert retried.payload["analysis_status"] == "complete"
    assert retried.payload["occurred_at"] == first.payload["occurred_at"]
    assert retried.payload["user_portion"] == "200 г"
    assert store.get(profile, newer.id).payload == newer_before  # type: ignore[union-attr]
    assert len(brain.calls) == calls_before_retry + 1
    assert brain.calls[-1][1] == photo.path

    calls_after_success = len(brain.calls)
    assert coach.handle(
        profile, "/порция 200 г", source_key="portion-retry", now=second_at,
    ) == answer
    assert len(brain.calls) == calls_after_success
