from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_carbs import classify, weekly

NOW = datetime(2026, 9, 13, 6, tzinfo=UTC)


def test_breakfast_notice_description_and_lunch_after_reminder():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    n = coach.due(profile, NOW)[0]
    store.put(
        profile, "food", "notice", n.key, {"delivered_at": NOW.isoformat()}, at=NOW
    )
    reply = coach.handle(
        profile,
        "Каша с миндалем и кокосовым молоком",
        source_key="breakfast",
        now=NOW + timedelta(minutes=2),
    )
    assert "новый приём" not in reply
    assert len(store.list(profile, "food", "meal")) == 1
    assert store.list(profile, "food", "meal")[0].payload["category"] == "breakfast"
    later = NOW + timedelta(hours=3, minutes=32)
    notice = next(n for n in coach.due(profile, later) if n.key.startswith("meal:"))
    store.put(
        profile,
        "food",
        "notice",
        notice.key,
        {"delivered_at": later.isoformat()},
        at=later,
    )
    lunch_at = NOW + timedelta(hours=3, minutes=51)
    reply = coach.handle(
        profile, "Покушал омлет с грибами и тост", source_key="lunch", now=lunch_at
    )
    assert "16:21" in reply
    assert len(store.list(profile, "food", "meal")) == 2
    assert store.list(profile, "food", "comment") == []
    coach.handle(
        profile, "Покушал омлет с грибами и тост", source_key="lunch", now=lunch_at
    )
    assert len(store.list(profile, "food", "meal")) == 2
    assert any(
        n.key.startswith("meal:")
        for n in coach.due(profile, lunch_at + timedelta(hours=3, minutes=30))
    )


@pytest.mark.parametrize(
    "text",
    [
        "Я еще не поел",
        "Буду кашу",
        "Хочу яичницу",
        "Каша будет позже",
        "Завтра съем блин",
    ],
)
def test_plans_and_negations_do_not_create_meals(text):
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    n = coach.due(profile, NOW)[0]
    store.put(
        profile, "food", "notice", n.key, {"delivered_at": NOW.isoformat()}, at=NOW
    )
    coach.handle(profile, text, source_key="plan", now=NOW + timedelta(minutes=1))
    assert not store.list(profile, "food", "meal")


def test_carbohydrates_not_all_starch_is_slow_and_no_invented_gi():
    c = classify(
        [
            "овсянка",
            "белый хлеб",
            "яблоко",
            "апельсиновый сок",
            "гречка",
            "сахар",
            "каша",
        ]
    )
    assert c["whole_grains_legumes"] == ["овсянка", "гречка"]
    assert c["refined_starch"] == ["белый хлеб"]
    assert c["whole_fruit"] == ["яблоко"]
    assert c["free_sugars"] == ["апельсиновый сок", "сахар"]
    assert c["unspecified_starch"] == ["каша"]
    assert not classify(["кокосовое молоко без сахара"])
    assert "whole_grains_legumes" not in classify(["паста 10 минут"])
    h = {"meals": [{"foods": ["сахар"], "analysis_available": True}]}
    message = weekly(h)
    assert len(message) <= 450 and "1 из 1" in message and "нутриент" not in message


def test_reclassified_comment_is_excluded_from_old_meal_and_replay_uses_new_meal():
    store, profile, brain = MemoryStore(), uuid4(), Brain()
    coach = FoodCoach(store, brain)
    coach.handle(profile, "/ел завтрак каша", source_key="first", now=NOW)
    breakfast = store.list(profile, "food", "meal")[0]
    store.put(
        profile,
        "food",
        "comment",
        "second",
        {
            "meal_id": breakfast.id,
            "text": "Покушал омлет",
            "superseded_by_meal": "second",
        },
        at=NOW + timedelta(hours=4),
    )
    coach._analyse(profile, breakfast)
    assert brain.calls[-1][0]["meal"]["comments"] == []
    coach._meal(profile, "Покушал омлет", "second", NOW + timedelta(hours=4), None)
    count = len(brain.calls)
    coach.handle(
        profile, "Покушал омлет", source_key="second", now=NOW + timedelta(hours=4)
    )
    assert len(brain.calls) == count
    assert len(store.list(profile, "food", "meal")) == 2
