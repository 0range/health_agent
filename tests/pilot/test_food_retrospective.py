import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_history import build_food_history

NOW = datetime(2026, 8, 4, 15, tzinfo=UTC)  # 18:00 Moscow


def setup():
    store, profile = MemoryStore(), uuid4()
    brain = Brain(json.dumps({"foods": ["яйца", "вафля", "груша"], "kcal": 400}))
    store.put(profile, "food", "settings", "protocol", {"meal_assessment_version": 1})
    return store, profile, brain, FoodCoach(store, brain)


def test_delayed_reports_keep_explicit_time_category_and_chronology():
    store, profile, _, coach = setup()
    coach.handle(
        profile,
        "Был завтрак в 08:15. Белки, вафля и груша",
        source_key="breakfast",
        now=NOW,
    )
    breakfast = store.by_source(profile, "food", "meal", "breakfast")
    assert breakfast is not None and breakfast.payload["occurred_at"].startswith(
        "2026-08-04T05:15:00"
    )
    assert breakfast.payload["captured_at"] == NOW.isoformat()
    coach.handle(
        profile,
        "Был обед в 12:45. Рыба и овощи",
        source_key="lunch",
        now=NOW + timedelta(seconds=20),
    )
    lunch = store.by_source(profile, "food", "meal", "lunch")
    assert lunch.payload["category"] == "lunch"
    at = NOW
    coach.handle(
        profile,
        "В 16:20 — полдний. Рыба, рис и печенье",
        source_key="afternoon",
        now=at + timedelta(minutes=1),
    )
    afternoon = store.by_source(profile, "food", "meal", "afternoon")
    assert afternoon.payload["category"] == "afternoon"
    assert afternoon.payload["category_source"] == "explicit_label"
    assert coach._latest_meal(profile).id == afternoon.id
    assert (
        len(
            build_food_history(store, profile, at + timedelta(minutes=2), days=1)[
                "meals"
            ]
        )
        == 3
    )
    assert not store.list(profile, "food", "pending_text")


def test_explicit_historical_label_beats_wrong_open_reminder():
    store, profile, _, coach = setup()
    coach.handle(
        profile, "/ел завтрак в 14:00 каша", source_key="late-breakfast", now=NOW
    )
    at = NOW + timedelta(minutes=1)
    notice = coach.due(profile, at)[0]
    store.put(
        profile, "food", "notice", notice.key, {"delivered_at": at.isoformat()}, at=at
    )
    coach.handle(
        profile,
        "В 16:10 — полдний. Рис и рыба",
        source_key="afternoon",
        now=at + timedelta(minutes=1),
    )
    assert (
        store.by_source(profile, "food", "meal", "afternoon").payload["category"]
        == "afternoon"
    )


def test_short_label_clarification_retains_pending_description_and_time():
    store, profile, _, coach = setup()
    store.put(
        profile,
        "food",
        "pending_text",
        "description",
        {"text": "В 08:15 белки, вафля и груша", "candidate_meal_id": None},
        at=NOW,
    )
    reply = coach.handle(
        profile,
        "Нет, завтрак",
        source_key="clarification",
        now=NOW + timedelta(seconds=10),
    )
    meals = store.list(profile, "food", "meal")
    assert len(meals) == 1 and meals[0].source_key == "description"
    assert meals[0].payload["original"] == "В 08:15 белки, вафля и груша"
    assert meals[0].payload["category"] == "breakfast" and "08:15" in reply
    assert not coach._pending_text(profile)
    assert (
        coach.handle(
            profile,
            "Нет, завтрак",
            source_key="clarification",
            now=NOW + timedelta(days=1),
        )
        == reply
    )


def test_short_correction_updates_most_recent_input_not_later_occurrence():
    store, profile, _, coach = setup()
    coach.handle(profile, "/ел обед в 13:00 рыба", source_key="lunch", now=NOW)
    coach.handle(
        profile,
        "/ел обед в 08:15 яйца",
        source_key="mistake",
        now=NOW + timedelta(seconds=1),
    )
    reply = coach.handle(
        profile, "Нет, завтрак", source_key="fix", now=NOW + timedelta(seconds=2)
    )
    assert "08:15" in reply
    assert (
        store.by_source(profile, "food", "meal", "mistake").payload["category"]
        == "breakfast"
    )
    assert (
        store.by_source(profile, "food", "meal", "lunch").payload["category"] == "lunch"
    )
    assert len(store.list(profile, "food", "meal")) == 2


def test_delayed_photo_uses_caption_time_for_meal_and_next_reminder(tmp_path):
    store, profile, _, coach = setup()
    photo = Attachment(tmp_path / "meal.jpg", "image/jpeg", "Завтрак в 08:15. Яйца")
    reply = coach.handle(profile, "", source_key="photo", now=NOW, attachment=photo)
    meal = store.list(profile, "food", "meal")[0]
    assert meal.payload["occurred_at"].startswith("2026-08-04T05:15:00")
    assert coach._meal_target(meal.payload, {}).hour < NOW.hour
    assert "08:15" in reply


def test_label_only_without_context_never_creates_empty_food():
    store, profile, _, coach = setup()
    coach.handle(profile, "Нет, завтрак", source_key="label", now=NOW)
    assert not store.list(profile, "food", "meal")
    assert not store.list(profile, "food", "pending_text")


def test_same_meal_label_on_followup_photo_preserves_photo_grouping(tmp_path):
    store, profile, _, coach = setup()
    photo = Attachment(tmp_path / "one.jpg", "image/jpeg", "Обед. Рыба")
    coach.handle(profile, photo.caption, source_key="one", now=NOW, attachment=photo)
    second = Attachment(tmp_path / "two.jpg", "image/jpeg", "Обед. Еще салат")
    coach.handle(
        profile,
        second.caption,
        source_key="two",
        now=NOW + timedelta(minutes=2),
        attachment=second,
    )
    meals = store.list(profile, "food", "meal")
    assert len(meals) == 1 and len(meals[0].payload["photos"]) == 2
    assert (
        store.by_source(profile, "food", "photo", "two").payload["meal_id"]
        == meals[0].id
    )
