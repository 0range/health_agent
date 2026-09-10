import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach


def test_breakfast_after_dinner_has_daily_receipt_and_no_late_catchup():
    store, profile, brain = MemoryStore(), uuid4(), Brain()
    coach = FoodCoach(store, brain)
    dinner = datetime(2026, 9, 9, 18, tzinfo=UTC)
    coach.handle(profile, "/ел ужин", source_key="dinner", now=dinner)
    morning = datetime(2026, 9, 10, 6, tzinfo=UTC)
    assert coach.due(profile, morning - timedelta(seconds=1)) == []
    notice = coach.due(profile, morning)[0]
    assert notice.key == "breakfast:2026-09-10"
    assert "завтрак" in notice.text.lower()
    # An attempted outbound send is not delivery; retry remains eligible.
    store.put(
        profile, "food", "outbound", notice.key, {"text": notice.text}, at=morning
    )
    assert FoodCoach(store, brain).due(profile, morning + timedelta(minutes=1))
    store.put(
        profile,
        "food",
        "notice",
        notice.key,
        {"delivered_at": morning.isoformat()},
        at=morning,
    )
    assert FoodCoach(store, brain).due(profile, morning + timedelta(minutes=2)) == []
    tomorrow = morning + timedelta(days=1)
    assert coach.due(profile, tomorrow)
    assert coach.due(profile, tomorrow + timedelta(hours=2)) == []


def test_breakfast_configuration_pause_quiet_and_already_eaten():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    morning = datetime(2026, 9, 10, 6, tzinfo=UTC)
    assert "09:30" in coach.handle(
        profile, "/завтрак 09:30", source_key="schedule", now=morning
    )
    assert coach.due(profile, morning) == []
    assert coach.due(profile, morning + timedelta(minutes=30))
    coach.handle(profile, "/завтрак выкл", source_key="breakfast-off", now=morning)
    assert coach.due(profile, morning + timedelta(minutes=30)) == []
    coach.handle(
        profile,
        "/завтрак вкл",
        source_key="breakfast-on",
        now=morning + timedelta(seconds=1),
    )
    coach.handle(profile, "/напоминания выкл", source_key="all-off", now=morning)
    assert coach.due(profile, morning + timedelta(minutes=30)) == []
    coach.handle(
        profile,
        "/напоминания вкл",
        source_key="all-on",
        now=morning + timedelta(seconds=1),
    )
    coach.handle(profile, "/ел завтрак овсянка", source_key="breakfast", now=morning)
    assert coach.due(profile, morning + timedelta(minutes=30)) == []
    assert "Укажите" in coach.handle(
        profile, "/завтрак 30:99", source_key="invalid", now=morning
    )
    other = uuid4()
    coach.handle(other, "/завтрак 06:30", source_key="quiet-schedule", now=morning)
    assert coach.due(other, morning.replace(hour=3, minute=30)) == []
    assert coach.due(other, morning.replace(hour=4, minute=0))


def test_planned_and_confirmed_bread_survive_model_omission_and_replay(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    brain = Brain(
        json.dumps(
            {
                "foods": ["курица", "салат"],
                "plate_components": ["protein", "vegetables"],
                "kcal": 280,
            }
        )
    )
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    store.put(
        profile,
        "food",
        "settings",
        "protocol",
        {"plate_rules": {"lunch": ["protein", "vegetables", "grains"]}},
    )
    coach.handle(
        profile,
        "Обед",
        source_key="photo",
        now=now,
        attachment=Attachment(tmp_path / "meal.jpg", "image/jpeg", "Обед"),
    )
    planned = coach.handle(
        profile, "Хлеб добавлю", source_key="planned", now=now + timedelta(minutes=1)
    )
    assert "хлеб" in planned.lower() and "план" in planned.lower()
    assert "можно добавить крупы или хлеб" not in planned
    meal = store.list(profile, "food", "meal")[0]
    assert "хлеб" not in meal.payload["analysis"]["foods"]
    assert meal.payload["analysis"]["kcal"] == 280
    confirmed = coach.handle(
        profile, "Добавил хлеб", source_key="confirmed", now=now + timedelta(minutes=2)
    )
    assert "Учтены дополнения: хлеб" in confirmed
    meal = store.list(profile, "food", "meal")[0]
    assert set(meal.payload["analysis"]["foods"]) == {"курица", "салат", "хлеб"}
    assert meal.payload["analysis"]["kcal"] is None  # old 280 did not include bread
    assert meal.payload["planned_additions"] == []
    assert meal.payload["occurred_at"] == now.isoformat()
    assert len(store.list(profile, "food", "meal")) == 1
    calls = len(brain.calls)
    assert (
        coach.handle(
            profile,
            "Добавил хлеб",
            source_key="confirmed",
            now=now + timedelta(minutes=2),
        )
        == confirmed
    )
    assert len(brain.calls) == calls


def test_caption_plans_and_failed_analysis_keep_addition_evidence(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain(RuntimeError("offline")))
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    reply = coach.handle(
        profile,
        "Вот обед, еще яблоко добавлю",
        source_key="photo",
        now=now,
        attachment=Attachment(
            tmp_path / "meal.jpg", "image/jpeg", "Вот обед, еще яблоко добавлю"
        ),
    )
    assert "яблоко" in reply and "план" in reply.lower()
    reply = coach.handle(
        profile, "Добавил хлеб", source_key="bread", now=now + timedelta(minutes=1)
    )
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["confirmed_additions"] == ["хлеб"]
    assert "Учтены дополнения: хлеб" in reply


def test_scalar_foods_and_uncertainty_from_provider_are_preserved():
    value = FoodCoach._parse_analysis(
        json.dumps(
            {
                "foods": "Овсянка с голубикой, торт",
                "unknowns": "Масса предполагается, а не измерена",
                "kcal": 550,
            }
        ),
        False,
    )
    assert value["foods"] == ["Овсянка с голубикой, торт"]
    assert value["unknowns"] == ["Масса предполагается, а не измерена"]


def test_uncertain_and_negative_statements_do_not_confirm_food():
    from health_agent.pilot.food_additions import additions, statement

    for text in (
        "Можно хлеб добавить?",
        "Может добавлю хлеб",
        "Если захочу добавлю хлеб",
        "Добавлю хлеб завтра",
    ):
        assert statement(text) is None
    assert additions(["Хлеб добавлю", "Хлеб не добавлю"])["planned_additions"] == []
    assert additions(["Не добавил хлеб"])["confirmed_additions"] == []
    assert (
        additions(["Хлеб добавлю", "Добавил ломтик хлеба"])["planned_additions"] == []
    )


def test_planned_bread_is_not_counted_as_eaten_when_model_includes_it(tmp_path):
    store, profile = MemoryStore(), uuid4()
    brain = Brain(
        json.dumps({"foods": ["курица"], "plate_components": ["protein"], "kcal": 280})
    )
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 10, 10, tzinfo=UTC)
    coach.handle(
        profile,
        "Обед",
        source_key="photo",
        now=now,
        attachment=Attachment(tmp_path / "meal.jpg", "image/jpeg", "Обед"),
    )
    brain.reply = json.dumps(
        {
            "foods": ["курица", "хлеб"],
            "plate_components": ["protein", "grains"],
            "kcal": 350,
        }
    )
    reply = coach.handle(
        profile, "Хлеб добавлю", source_key="plan", now=now + timedelta(minutes=1)
    )
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["foods"] == ["курица"]
    assert "grains" not in analysis["plate_components"]
    assert analysis["kcal"] is None
    assert "В плане добавить: хлеб" in reply
