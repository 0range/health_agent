import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_food import MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_additions import additions, grain_corrections, reconcile
from health_agent.pilot.food_carbs import classify


class SequenceBrain:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def __call__(self, system, payload, *, image_path=None):
        self.calls.append((payload, image_path))
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return json.dumps(reply, ensure_ascii=False)


@pytest.mark.parametrize("prefix", ["Еще ", "Ещё ", "И ещё ", "А еще ", "+ "])
def test_short_list_is_separate_confirmed_foods(prefix):
    assert additions([prefix + "зефирка и печенька"])["confirmed_additions"] == [
        "зефирка",
        "печенька",
    ]


@pytest.mark.parametrize(
    "text",
    [
        "Ещё хочу печеньку",
        "Еще не ел хлеб",
        "Еще печеньку?",
        "Еще завтра яблоко",
        "Еще про хлеб",
        "Еще о хлебе",
        "Еще напомни про обед",
        "Еще спасибо",
        "Еще куплю яблоко",
        "Еще буду есть хлеб",
    ],
)
def test_short_nonconsumption_is_not_confirmed(text):
    assert additions([text])["confirmed_additions"] == []


def test_diminutives_do_not_duplicate_model_foods_or_calories():
    result = reconcile(
        {"foods": ["зефир", "печенье"], "kcal": 170},
        additions(["Еще зефирка и печенька", "+ зефир"]),
        {},
        ("kcal",),
    )
    assert result == {"foods": ["зефир", "печенье"], "kcal": 170}
    assert classify(["зефирка", "печенька", "печенье", "печень"])["free_sugars"] == [
        "зефирка",
        "печенька",
        "печенье",
    ]


@pytest.mark.parametrize(
    "retry, expected_kcal",
    [
        ({"foods": ["курица", "салат", "зефир", "печенье"], "kcal": 490}, 490),
        ({"foods": ["курица", "салат"], "kcal": 320}, None),
        ({"foods": ["зефир", "печенье"], "kcal": 170}, None),
        (RuntimeError("offline"), None),
        ({"invalid": True}, None),
    ],
)
def test_omitted_additions_retry_whole_meal_and_preserve_time(
    tmp_path: Path, retry, expected_kcal
):
    original = {"foods": ["курица", "салат"], "kcal": 320}
    brain = SequenceBrain([original, original, retry])
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 8, 1, 9, tzinfo=UTC)
    photo = Attachment(tmp_path / "plate.jpg", "image/jpeg", "Обед")
    coach.handle(profile, "Обед", source_key="photo", now=now, attachment=photo)
    before = store.list(profile, "food", "meal")[0]
    notice = coach.due(profile, now + timedelta(hours=3, minutes=50))[0].key
    reply = coach.handle(
        profile,
        "Еще зефирка и печенька",
        source_key="addition",
        now=now + timedelta(minutes=4),
    )
    after = store.list(profile, "food", "meal")[0]
    assert after.id == before.id
    assert after.payload["occurred_at"] == before.payload["occurred_at"]
    assert after.payload["analysis"]["kcal"] == expected_kcal
    assert len(after.payload["analysis"]["foods"]) == 4
    assert {"курица", "салат"} <= set(after.payload["analysis"]["foods"])
    assert after.payload["confirmed_additions"] == ["зефирка", "печенька"]
    assert brain.calls[-1][1] is None
    assert brain.calls[-1][0]["meal"]["authoritative_foods"] == [
        "курица",
        "салат",
        "зефирка",
        "печенька",
    ]
    assert coach.due(profile, now + timedelta(hours=3, minutes=50))[0].key == notice
    assert after.payload["analysis_attempts"][-1]["mode"] == "text_reconciliation"
    assert (
        coach.handle(
            profile,
            "Еще зефирка и печенька",
            source_key="addition",
            now=now + timedelta(minutes=4),
        )
        == reply
    )
    assert len(brain.calls) == 3
    assert len(store.list(profile, "food", "meal")) == 1


def test_grain_correction_replaces_single_grain_and_retains_caption(tmp_path: Path):
    original = {"foods": ["курица", "рис отварной", "яблоко"], "kcal": 400}
    revised = {"foods": ["курица", "булгур", "яблоко"], "kcal": 410}
    brain = SequenceBrain([original, original, revised])
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, brain)
    now = datetime(2026, 8, 1, 9, tzinfo=UTC)
    coach.handle(
        profile,
        "+ яблоко",
        source_key="photo",
        now=now,
        attachment=Attachment(tmp_path / "plate.jpg", "image/jpeg", "+ яблоко"),
    )
    reply = coach.handle(
        profile, "Булгур это", source_key="correction", now=now + timedelta(minutes=1)
    )
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["analysis"]["foods"] == revised["foods"]
    assert meal.payload["analysis"]["kcal"] == 410
    assert meal.payload["confirmed_additions"] == ["яблоко"]
    assert "Исправлен состав: булгур." in reply
    assert grain_corrections(["Булгур это"], ["рис", "гречка"]) == []
    assert grain_corrections(["Булгур это"], ["суп с рисом"]) == []


def test_cookie_is_not_mistaken_for_liver():
    result = reconcile(
        {"foods": ["печень"], "kcal": 150},
        additions(["Еще печенька"]),
        {},
        ("kcal",),
    )
    assert result["foods"] == ["печень", "печенька"]
    assert result["kcal"] is None
