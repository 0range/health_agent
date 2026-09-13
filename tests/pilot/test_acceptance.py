from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.sleep import SleepCoach
from health_agent.pilot.storage import PilotStore
from health_agent.pilot.training import TrainingCoach

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)  # 12:00 in Moscow


class FakeBrain:
    """Schema-aware deterministic model double; no network or private data."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], Path | None]] = []

    def __call__(
        self,
        system: str,
        payload: dict[str, Any],
        *,
        image_path: Path | None = None,
    ) -> str:
        self.calls.append((system, payload, image_path))
        task = payload.get("task")
        if task == "weekly_reflection":
            ids = [entry["id"] for entry in payload["weekly_entries"]]
            return json.dumps(
                {
                    "observations": [
                        {"text": "Есть три записи сна", "entry_ids": ids}
                    ],
                    "hypotheses": [
                        {
                            "text": "Режим мог влиять на бодрость",
                            "entry_ids": ids[:1],
                        }
                    ],
                    "next_question": "Что отличало самое бодрое утро?",
                },
                ensure_ascii=False,
            )
        if task == "weekly_plan":
            return "Черновик: две лёгкие тренировки и отдых. Какой объём привычен?"
        if task == "reflection":
            return "Факт: выполнена одна пробежка; вывод основан на данных COROS."
        if "meal" in payload:
            return json.dumps(
                {
                    "foods": ["рис", "овощи"],
                    "portion_estimate": "одна тарелка",
                    "kcal": None,
                    "protein_g": None,
                    "fat_g": None,
                    "carbs_g": None,
                    "saturated_fat_g": None,
                    "fiber_g": None,
                    "cholesterol_mg": None,
                    "confidence": 0.5,
                    "unknowns": ["масса продуктов"],
                    "feedback": "В записи есть рис и овощи.",
                },
                ensure_ascii=False,
            )
        return "Помню только разговор о сне."


class FakeCoros:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, datetime, datetime]] = []

    def __call__(self, profile_id, since: datetime, until: datetime):
        self.calls.append((profile_id, since, until))
        return [
            {
                "id": "coros-run-1",
                "sport": "run",
                "started_at": "2026-09-06T08:00:00+00:00",
                "duration_s": 1800,
                "source": "fake_coros",
            }
        ]


def test_sleep_journey_survives_restart_and_builds_structured_weekly_summary(
    clean_database,
):
    brain = FakeBrain()
    coach = SleepCoach(PilotStore(clean_database), brain)
    for day in range(3):
        coach.handle(
            DEFAULT_PROFILE_ID,
            f"/сон Запись сна {day}",
            source_key=f"sleep-{day}",
            now=NOW + timedelta(days=day),
        )
    coach.handle(
        DEFAULT_PROFILE_ID,
        "Вчера мы обсуждали сон?",
        source_key="sleep-follow-up",
        now=NOW + timedelta(days=3),
    )
    assert "Запись сна 2" in str(brain.calls[-1][1]["diary_user_reports"])

    restarted_brain = FakeBrain()
    restarted = SleepCoach(PilotStore(clean_database), restarted_brain)
    summary = restarted.handle(
        DEFAULT_PROFILE_ID,
        "/итоги",
        source_key="sleep-weekly",
        now=datetime(2026, 9, 13, 16, tzinfo=UTC),
    )
    assert "Наблюдение:" in summary
    assert "Гипотеза (не установленная причина):" in summary
    weekly = restarted_brain.calls[-1][1]["weekly_entries"]
    assert len(weekly) == 3
    assert {entry["user_report"] for entry in weekly} == {
        "Запись сна 0",
        "Запись сна 1",
        "Запись сна 2",
    }


def test_food_journey_reminder_correction_restart_and_local_day_separation(
    clean_database,
):
    brain = FakeBrain()
    coach = FoodCoach(PilotStore(clean_database), brain)
    coach.handle(
        DEFAULT_PROFILE_ID,
        "/ел 12:00 Обед: рис и овощи",
        source_key="food-lunch",
        now=NOW,
    )
    assert coach.due(DEFAULT_PROFILE_ID, NOW + timedelta(hours=3, minutes=30))

    coach.handle(
        DEFAULT_PROFILE_ID,
        "/время 13:00",
        source_key="food-correction",
        now=NOW + timedelta(hours=1),
    )
    assert coach.due(DEFAULT_PROFILE_ID, NOW + timedelta(hours=3, minutes=30)) == []
    assert coach.due(DEFAULT_PROFILE_ID, NOW + timedelta(hours=4, minutes=30))

    next_day = NOW + timedelta(days=1)
    restarted = FoodCoach(PilotStore(clean_database), FakeBrain())
    restarted.handle(
        DEFAULT_PROFILE_ID,
        "/ел 12:00 Новый обед",
        source_key="food-next-day",
        now=next_day,
    )
    today = restarted.handle(
        DEFAULT_PROFILE_ID,
        "/сегодня",
        source_key="food-today",
        now=next_day,
    )
    week = restarted.handle(
        DEFAULT_PROFILE_ID,
        "/неделя",
        source_key="food-week",
        now=next_day,
    )
    assert "Сохранено приёмов пищи: 1" in today
    assert "2 из 2" in week
    assert len(week) <= 450
    meals = PilotStore(clean_database).list(DEFAULT_PROFILE_ID, "food", "meal")
    corrected = next(meal for meal in meals if meal.source_key == "food-lunch")
    assert corrected.payload["occurred_at"] == "2026-09-07T10:00:00+00:00"


def test_shared_goal_training_acceptance_fake_coros_and_domain_chat_isolation(
    clean_database,
):
    store = PilotStore(clean_database)
    store.put(
        DEFAULT_PROFILE_ID,
        "shared",
        "goal",
        "annual-running-goal",
        {
            "domain": "training",
            "text": "Бегать регулярно",
            "events": [{"name": "осенний старт", "period": "осень 2027"}],
        },
        at=NOW,
    )
    sleep_brain = FakeBrain()
    SleepCoach(store, sleep_brain).handle(
        DEFAULT_PROFILE_ID,
        "Сонный разговор без тренировок",
        source_key="sleep-private-chat",
        now=NOW,
    )
    assert sleep_brain.calls[-1][1]["goals_not_evidence"][0]["text"] == (
        "Бегать регулярно"
    )

    training_brain, coros = FakeBrain(), FakeCoros()
    coach = TrainingCoach(store, training_brain, activity_source=coros)
    assert "Бегать регулярно" in coach.handle(
        DEFAULT_PROFILE_ID, "/год", source_key="training-year", now=NOW
    )
    proposal = coach.handle(
        DEFAULT_PROFILE_ID, "/план", source_key="training-proposal", now=NOW
    )
    assert proposal
    assert store.list(DEFAULT_PROFILE_ID, "training", "accepted_plan") == []
    coach.handle(
        DEFAULT_PROFILE_ID,
        "/сохранить план",
        source_key="training-accept",
        now=NOW,
    )
    reflection = coach.handle(
        DEFAULT_PROFILE_ID,
        "/итоги",
        source_key="training-reflection",
        now=NOW,
    )
    assert "COROS" in reflection
    assert coros.calls
    assert store.list(DEFAULT_PROFILE_ID, "training", "activity")[0].payload[
        "source"
    ] == "fake_coros"

    restarted_brain = FakeBrain()
    restarted = TrainingCoach(
        PilotStore(clean_database), restarted_brain, activity_source=coros
    )
    restarted.handle(
        DEFAULT_PROFILE_ID,
        "Продолжим про бег",
        source_key="training-private-chat",
        now=NOW + timedelta(minutes=1),
    )
    training_payload = restarted_brain.calls[-1][1]
    assert "Сонный разговор" not in str(training_payload)
    assert len(store.list(DEFAULT_PROFILE_ID, "training", "accepted_plan")) == 1
