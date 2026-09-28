import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot import food_snacks
from health_agent.pilot.brain import _selected_food_framework
from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_history import build_food_history

PROTOCOL = {"meal_assessment_version": 1, "bridge_snack": {"enabled": True}}
NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)  # 13:00 Moscow


def setup():
    store, profile = MemoryStore(), uuid4()
    store.put(profile, "food", "settings", "protocol", PROTOCOL)
    brain = Brain(json.dumps({"foods": ["10 орехов миндаля"], "kcal": 70}))
    return store, profile, brain, FoodCoach(store, brain)


@pytest.mark.parametrize("text", [
    "съел 10 орехов", "Съел 10 орехов миндаля", "10 миндалин", "перекус: 7 орехов",
    "съела 10 штук фундука", "в 13:00 съел 10 миндальных орехов",
    "съел 5 половинок грецкого ореха", "съел 10 половинок грецких орехов",
    "съел 5 грецких орехов", "/перекус 10 орехов", "съел десять орехов",
])
def test_standalone_small_nuts_are_snacks(text):
    assert food_snacks.recognizes(text, PROTOCOL)
    assert not food_snacks.recognizes(text, {})


@pytest.mark.parametrize("text", [
    "завтрак: каша и 10 орехов", "обед: 10 орехов", "полдник: 10 орехов",
    "добавил 10 орехов", "ещё 10 орехов", "съел кашу и 10 орехов",
    "не съел 10 орехов", "съем 10 орехов", "хочу 10 орехов", "можно 10 орехов?",
    "съел 100 орехов", "съел 10 грецких орехов", "10 орехов кешью",
    "10 фисташек", "10 орехов макадамии", "10 орехов арахиса", "10 орехов и яблоко",
    "10 г миндаля", "110 миндалин", "10.5 орехов", "планирую перекус 10 орехов",
])
def test_do_not_reclassify_other_food_or_intentions(text):
    assert not food_snacks.recognizes(text, PROTOCOL)


def log_breakfast(coach, profile):
    coach.handle(profile, "завтрак: овсянка и яблоко", source_key="breakfast", now=NOW - timedelta(hours=4))


def receipt(store, profile, notice, now):
    store.put(profile, "food", "notice", notice.key, {"delivered_at": now.isoformat()}, at=now)


def test_snack_preserves_pending_lunch_and_replay_and_full_meal_stops_reminders():
    store, profile, brain, coach = setup()
    log_breakfast(coach, profile)
    delivered = NOW - timedelta(minutes=30)
    receipt(store, profile, coach.due(profile, delivered)[0], delivered)
    reply = coach.handle(profile, "съел 10 орехов миндаля", source_key="snack", now=NOW)
    snack = store.list(profile, "food", "meal")[0]
    assert snack.payload["category"] == "bridge_snack"
    assert snack.payload["next_meal_category"] == "lunch"
    assert "перекус" in reply.lower() and "≈70 ккал" in reply
    assert "14:00–14:30" in reply and "обед" in reply.lower() and "⚠️" not in reply
    assert coach.due(profile, NOW + timedelta(minutes=59)) == []
    first = FoodCoach(store, brain).due(profile, NOW + timedelta(hours=1))[0]
    assert "обед" in first.text and snack.id in first.key
    coach.handle(profile, "съел 10 орехов миндаля", source_key="snack", now=NOW)
    assert len(store.list(profile, "food", "meal")) == 2
    receipt(store, profile, first, NOW + timedelta(hours=1))
    coach.handle(profile, "съел рыбу и рис", source_key="lunch", now=NOW + timedelta(minutes=80))
    assert store.list(profile, "food", "meal")[0].payload["category"] == "lunch"
    assert coach.due(profile, NOW + timedelta(minutes=90)) == []


def test_three_reminders_controls_and_expiry():
    store, profile, brain, coach = setup()
    log_breakfast(coach, profile)
    coach.handle(profile, "10 миндалин", source_key="snack", now=NOW)
    for n, minutes in enumerate((60, 90, 120), 1):
        at = NOW + timedelta(minutes=minutes)
        notices = FoodCoach(store, brain).due(profile, at)
        assert len(notices) == 1 and f"{n}/3" in notices[0].text
        receipt(store, profile, notices[0], at)
        assert coach.due(profile, at + timedelta(minutes=29)) == []
    assert coach.due(profile, NOW + timedelta(minutes=150)) == []
    _, p, _, c = setup()
    c.handle(p, "10 миндалин", source_key="s", now=NOW)
    assert c.due(p, NOW + timedelta(minutes=151)) == []
    assert "не могу" in c.handle(p, "/позже 200", source_key="late", now=NOW).lower()


@pytest.mark.parametrize("command", ["/пропустить", "уже ел", "/напоминания выкл"])
def test_stop_commands_apply_to_snack_reminder(command):
    store, profile, _, coach = setup()
    log_breakfast(coach, profile)
    coach.handle(profile, "10 миндалин", source_key="s", now=NOW)
    at = NOW + timedelta(hours=1)
    receipt(store, profile, coach.due(profile, at)[0], at)
    coach.handle(profile, command, source_key="control", now=at + timedelta(minutes=1))
    assert coach.due(profile, at + timedelta(minutes=30)) == []


def test_explicit_time_correction_and_snooze():
    _, profile, _, coach = setup()
    log_breakfast(coach, profile)
    reply = coach.handle(profile, "в 12:30 съел 10 орехов", source_key="s", now=NOW)
    assert "13:30–14:00" in reply
    coach.handle(profile, "/время 12:45", source_key="time", now=NOW)
    assert not coach.due(profile, NOW + timedelta(minutes=44))
    assert coach.due(profile, NOW + timedelta(minutes=45))
    coach.handle(profile, "/позже 30", source_key="snooze", now=NOW + timedelta(minutes=45))
    assert not coach.due(profile, NOW + timedelta(minutes=60))
    assert coach.due(profile, NOW + timedelta(minutes=75))


@pytest.mark.parametrize("photo", [False, True])
def test_early_full_meal_after_snack_keeps_next_slot_and_is_not_an_addition(photo, tmp_path):
    store, profile, _, coach = setup()
    log_breakfast(coach, profile)
    coach.handle(profile, "10 миндалин", source_key="s", now=NOW)
    attachment = Attachment(tmp_path / "lunch.jpg", "image/jpeg", "") if photo else None
    coach.handle(profile, "" if photo else "рис и рыба", source_key="full", now=NOW + timedelta(minutes=20), attachment=attachment)
    meals = store.list(profile, "food", "meal")
    assert len(meals) == 3 and meals[0].payload["category"] == "lunch"


def test_morning_snack_owns_breakfast_timing_without_duplicate_notices():
    store, profile, _, coach = setup()
    at = NOW.replace(hour=5, minute=30)  # 08:30
    coach.handle(profile, "10 миндалин", source_key="s", now=at)
    assert coach.due(profile, at + timedelta(minutes=30)) == []
    notices = coach.due(profile, at + timedelta(minutes=60))
    assert len(notices) == 1 and "завтрак" in notices[0].text
    receipt(store, profile, notices[0], at + timedelta(minutes=60))
    coach.handle(profile, "/позже 30", source_key="later", now=at + timedelta(minutes=65))
    assert coach.due(profile, at + timedelta(minutes=90)) == []
    assert len(coach.due(profile, at + timedelta(minutes=95))) == 1


def test_calories_count_but_snack_is_not_a_full_meal_in_day_week_or_shared_context():
    store, profile, brain, coach = setup()
    brain.reply = json.dumps({"foods": ["овсянка", "яблоко"], "kcal": 350})
    log_breakfast(coach, profile)
    brain.reply = json.dumps({"foods": ["10 миндалин"], "kcal": 70})
    coach.handle(profile, "10 миндалин", source_key="s", now=NOW)
    day = coach.handle(profile, "/сегодня", source_key="day", now=NOW)
    assert "≈420 ккал" in day and "1 из 4" in day and "Перекусы" in day
    history = build_food_history(store, profile, NOW, days=1)
    assert history["recorded_meal_count"] == 2
    assert history["full_meal_count"] == 1 and history["bridge_snack_count"] == 1
    assert history["meals"][0]["category"] == "bridge_snack"
    week = coach.handle(profile, "/неделя", source_key="week", now=NOW)
    assert "перекус" in week.lower() and "все 4 приёма есть в 0" in week
    assert _selected_food_framework(PROTOCOL)["bridge_snack"]["followup_minutes"] == [60, 90]


def test_model_failure_keeps_snack_and_reminder_without_invented_calories():
    _, profile, brain, coach = setup()
    brain.reply = RuntimeError("offline")
    reply = coach.handle(profile, "10 миндалин", source_key="s", now=NOW)
    assert "неизвестны" in reply and "14:00–14:30" in reply
    assert coach.due(profile, NOW + timedelta(minutes=60))
    assert not coach.due(uuid4(), NOW + timedelta(minutes=60))


def test_caption_snack_has_no_twenty_minute_photo_delay(tmp_path: Path):
    store, profile, _, coach = setup()
    log_breakfast(coach, profile)
    photo = Attachment(tmp_path / "nuts.jpg", "image/jpeg", "съел 10 миндалин")
    reply = coach.handle(profile, "", source_key="photo", now=NOW, attachment=photo)
    assert "14:00–14:30" in reply
    assert store.list(profile, "food", "meal")[0].payload["category"] == "bridge_snack"
    coach.handle(profile, "/время 12:50", source_key="time", now=NOW)
    assert coach.due(profile, NOW + timedelta(minutes=50))


def test_late_first_delivery_does_not_extend_snack_reminders_past_deadline():
    store, profile, _, coach = setup()
    coach.handle(profile, "10 миндалин", source_key="s", now=NOW)
    late = NOW + timedelta(minutes=140)
    receipt(store, profile, coach.due(profile, late)[0], late)
    assert not coach.due(profile, NOW + timedelta(minutes=170))
    assert "не могу" in coach.handle(profile, "/позже 30", source_key="later", now=late).lower()


def test_nuts_added_to_breakfast_stay_with_breakfast():
    store, profile, _, coach = setup()
    log_breakfast(coach, profile)
    coach.handle(profile, "добавил 10 орехов", source_key="addition", now=NOW - timedelta(hours=3, minutes=45))
    meals = store.list(profile, "food", "meal")
    assert len(meals) == 1 and meals[0].payload["category"] == "breakfast"
    assert store.list(profile, "food", "comment")[0].payload["meal_id"] == meals[0].id


def test_later_explicit_dinner_overrides_pending_afternoon_and_quiet_hours_hold():
    store, profile, _, coach = setup()
    coach.handle(profile, "обед: рис и рыба", source_key="lunch", now=NOW)
    snack_at = NOW + timedelta(hours=4)
    coach.handle(profile, "10 миндалин", source_key="s", now=snack_at)
    snack = store.list(profile, "food", "meal")[0]
    assert snack.payload["next_meal_category"] == "afternoon"
    coach.handle(profile, "ужин: рыба", source_key="dinner", now=snack_at + timedelta(hours=1))
    assert store.list(profile, "food", "meal")[0].payload["category"] == "dinner"
    assert not coach.due(profile, snack_at + timedelta(hours=1, minutes=30))
    late = NOW.replace(hour=18, minute=30)  # 21:30
    coach.handle(profile, "10 миндалин", source_key="late", now=late)
    assert not coach.due(profile, late + timedelta(hours=1))


def test_breakfast_receipt_before_retrospectively_logged_snack_is_not_active():
    store, profile, _, coach = setup()
    at = NOW.replace(hour=6)  # 09:00
    receipt(store, profile, coach.due(profile, at)[0], at)
    coach.handle(profile, "в 08:30 съел 10 орехов", source_key="s", now=at + timedelta(minutes=20))
    assert coach._active_meal_notice(profile, at + timedelta(minutes=20)) is None
    notices = coach.due(profile, at + timedelta(minutes=30))
    assert len(notices) == 1 and "завтрак" in notices[0].text


def test_protocol_is_opt_in_and_help_does_not_log_food():
    store, profile, brain, coach = setup()
    assert "1–1,5" in coach.handle(profile, "/перекус", source_key="help", now=NOW)
    assert not store.list(profile, "food", "meal") and not brain.calls
    other = uuid4()
    coach.handle(other, "съел 10 орехов", source_key="s", now=NOW)
    assert store.list(other, "food", "meal")[0].payload["category"] == "lunch"
