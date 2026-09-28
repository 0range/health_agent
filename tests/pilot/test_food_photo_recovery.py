import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_food import MemoryStore
from test_food_journey import SequenceBrain

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach

NOW = datetime(2026, 9, 28, 10, 52, tzinfo=UTC)
PLATE = ["гречка", "рыба", "овощи"]


def estimate(foods, kcal, *, scope="whole_meal"):
    return json.dumps({"foods": foods, "kcal": kcal, "analysis_scope": scope,
                       "portion_estimate": "порция", "unknowns": ["вес оценочный"]})


def setup(replies):
    store, profile = MemoryStore(), uuid4()
    store.put(profile, "food", "settings", "protocol", {"meal_assessment_version": 1})
    brain = SequenceBrain(replies)
    return store, profile, brain, FoodCoach(store, brain)


def photo(coach, profile, tmp_path, key, minutes):
    return coach.handle(profile, "", source_key=key, now=NOW+timedelta(minutes=minutes),
                        attachment=Attachment(tmp_path / (key+".jpg"), "image/jpeg", ""))


def test_apple_whole_meal_total_survives_second_photo_and_repeated_view(tmp_path):
    store, profile, brain, coach = setup([
        estimate(PLATE, 600), estimate([*PLATE, "яблоко"], 704),
        estimate([*PLATE, "яблоко"], 704),
    ])
    photo(coach, profile, tmp_path, "plate", 0)
    before = store.list(profile, "food", "meal")[0]
    reply = photo(coach, profile, tmp_path, "apple", 23)
    assert "704" in reply and "неизвестны" not in reply
    meal = store.get(profile, before.id)
    assert meal.payload["analysis"]["kcal"] == 704
    assert meal.payload["photos"][0]["analysis"]["kcal"] == 600
    assert meal.payload["occurred_at"] == before.payload["occurred_at"]
    photo(coach, profile, tmp_path, "same-apple", 25)
    assert len(store.list(profile, "food", "meal")) == 1
    assert store.get(profile, before.id).payload["analysis"]["kcal"] == 704
    assert len(brain.calls) == 3


@pytest.mark.parametrize("scope", ["whole_meal", "photo", None])
def test_apple_only_or_unscoped_result_cannot_replace_whole_meal_total(tmp_path, scope):
    foods = ["яблоко"] if scope else [*PLATE, "яблоко"]
    store, profile, _, coach = setup([estimate(PLATE, 600), estimate(foods, 104, scope=scope)])
    photo(coach, profile, tmp_path, "plate", 0)
    photo(coach, profile, tmp_path, "apple", 23)
    meal = store.list(profile, "food", "meal")[0]
    assert set(meal.payload["analysis"]["foods"]) >= {*PLATE, "яблоко"}
    assert meal.payload["analysis"]["kcal"] is None


@pytest.mark.parametrize("failure", [RuntimeError("private provider error"), "not JSON"])
def test_failed_second_photo_keeps_previous_estimate_and_replay_recovers_same_meal(tmp_path, failure):
    store, profile, brain, coach = setup([
        estimate(PLATE, 600), failure, estimate([*PLATE, "яблоко"], 704),
    ])
    photo(coach, profile, tmp_path, "plate", 0)
    initial = store.list(profile, "food", "meal")[0]
    reply = photo(coach, profile, tmp_path, "apple", 23)
    failed = store.get(profile, initial.id)
    assert failed.payload["analysis_status"] == "incomplete"
    assert failed.payload["last_successful_analysis"]["kcal"] == 600
    assert "600" in reply and "предыдущ" in reply.lower()
    assert "ещё не" in reply and "private provider error" not in reply
    photo(coach, profile, tmp_path, "apple", 23)
    repaired = store.get(profile, initial.id)
    assert repaired.payload["analysis"]["kcal"] == 704
    assert len(repaired.payload["photos"]) == 2
    assert len(store.list(profile, "food", "meal")) == 1
    assert len(brain.calls) == 3


def test_unrecognized_prior_photo_prevents_claiming_a_complete_total(tmp_path):
    store, profile, _, coach = setup([RuntimeError("offline"), estimate(["яблоко"], 104)])
    photo(coach, profile, tmp_path, "plate", 0)
    photo(coach, profile, tmp_path, "apple", 23)
    analysis = store.list(profile, "food", "meal")[0].payload["analysis"]
    assert analysis["kcal"] is None
    assert analysis["analysis_scope"] == "partial"
