from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_food import Brain, MemoryStore

from health_agent.pilot.food import FoodCoach

NOW = datetime(2026, 9, 14, 6, tzinfo=UTC)  # Monday 09:00 Moscow


def setup_meal():
    store, profile, brain = MemoryStore(), uuid4(), Brain()
    coach = FoodCoach(store, brain)
    coach.handle(profile, "/ел завтрак каша", source_key="meal", now=NOW)
    return store, profile, coach


def deliver(store, profile, notice, at):
    store.put(
        profile,
        "food",
        "notice",
        notice.key,
        {"text": notice.text, "delivered_at": at.isoformat()},
        at=at,
    )


def test_three_notifications_actual_spacing_jitter_restart_and_no_fourth():
    store, profile, coach = setup_meal()
    first_at = NOW + timedelta(hours=3, minutes=30, seconds=12)
    notice = coach.due(profile, first_at)[0]
    assert "1/3" in notice.text and "обед" in notice.text
    store.put(
        profile, "food", "outbound", notice.key, {"text": notice.text}, at=first_at
    )
    assert coach.due(profile, first_at)[0] == notice  # failed send is retryable
    deliver(store, profile, notice, first_at)
    second_at = first_at + timedelta(minutes=30, seconds=9)
    restarted = FoodCoach(store, Brain())
    assert not restarted.due(profile, first_at + timedelta(minutes=29, seconds=59))
    second = restarted.due(profile, second_at)[0]
    assert second.key.endswith(":repeat:2")
    deliver(store, profile, second, second_at)
    third_at = second_at + timedelta(minutes=30, seconds=8)
    third = restarted.due(profile, third_at)[0]
    assert third.key.endswith(":repeat:3")  # not lost at old 4.5-hour cutoff
    deliver(store, profile, third, third_at)
    assert not restarted.due(profile, third_at + timedelta(minutes=30))
    assert not restarted.due(profile, third_at + timedelta(days=1))
    assert "три" in restarted.handle(
        profile, "/позже 30", source_key="after-cap", now=third_at
    )


def test_new_meal_stops_repeats_and_starts_next_category():
    store, profile, coach = setup_meal()
    first = NOW + timedelta(hours=3, minutes=30)
    deliver(store, profile, coach.due(profile, first)[0], first)
    lunch = first + timedelta(minutes=10)
    coach.handle(profile, "Суп и хлеб", source_key="lunch", now=lunch)
    assert len(store.list(profile, "food", "meal")) == 2
    assert not coach.due(profile, first + timedelta(minutes=30))
    notice = coach.due(profile, lunch + timedelta(hours=3, minutes=30))[0]
    assert "полдник" in notice.text and "1/3" in notice.text


def test_restart_does_not_burst_and_skip_pause_quiet_stop():
    store, profile, coach = setup_meal()
    first = NOW + timedelta(hours=3, minutes=30)
    deliver(store, profile, coach.due(profile, first)[0], first)
    late = first + timedelta(minutes=65)
    second = FoodCoach(store, Brain()).due(profile, late)[0]
    deliver(store, profile, second, late)
    assert not coach.due(profile, late + timedelta(seconds=1))
    coach.handle(profile, "/пропустить", source_key="skip", now=late)
    assert not coach.due(profile, late + timedelta(minutes=30))
    store2, other, other_coach = setup_meal()
    deliver(store2, other, other_coach.due(other, first)[0], first)
    other_coach.handle(other, "/напоминания выкл", source_key="off", now=first)
    assert not other_coach.due(other, first + timedelta(minutes=30))
    evening = NOW.replace(
        hour=14, minute=15
    )  # meal17:15, notice20:45, second21:15, third21:45
    other_coach.handle(other, "/напоминания вкл", source_key="on", now=evening)
    other_coach.handle(other, "/ел обед суп", source_key="evening", now=evening)
    target = evening + timedelta(hours=3, minutes=30)
    deliver(store2, other, other_coach.due(other, target)[0], target)
    assert not other_coach.due(other, NOW.replace(hour=19))  # 22:00 quiet


def test_snooze_does_not_reset_cap_and_legacy_receipt_is_first():
    store, profile, coach = setup_meal()
    first = NOW + timedelta(hours=3, minutes=30)
    notice = coach.due(profile, first)[0]
    store.put(profile, "food", "notice", notice.key, {"delivered": True}, at=first)
    coach.handle(
        profile, "/позже 45", source_key="snooze", now=first + timedelta(minutes=5)
    )
    assert not coach.due(profile, first + timedelta(minutes=30))
    second_at = first + timedelta(minutes=50)
    second = coach.due(profile, second_at)[0]
    assert "2/3" in second.text
    deliver(store, profile, second, second_at)
    third = coach.due(profile, second_at + timedelta(minutes=30))[0]
    assert "3/3" in third.text


def test_breakfast_repeat_snooze_skip_and_log():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    first = coach.due(profile, NOW)[0]
    deliver(store, profile, first, NOW)
    coach.handle(profile, "/позже 45", source_key="snooze-breakfast", now=NOW)
    assert not coach.due(profile, NOW + timedelta(minutes=30))
    at = NOW + timedelta(minutes=45)
    second = coach.due(profile, at)[0]
    assert "2/3" in second.text and "завтрак" in second.text
    deliver(store, profile, second, at)
    coach.handle(profile, "/пропустить", source_key="skip-breakfast", now=at)
    assert not coach.due(profile, at + timedelta(minutes=30))
    tomorrow = NOW + timedelta(days=1)
    deliver(store, profile, coach.due(profile, tomorrow)[0], tomorrow)
    coach.handle(
        profile,
        "Овсянка и сыр",
        source_key="breakfast",
        now=tomorrow + timedelta(minutes=5),
    )
    assert not coach.due(profile, tomorrow + timedelta(minutes=30))


def test_four_and_half_hour_plan_has_initial_delivery_grace():
    store, profile, coach = setup_meal()
    store.put(profile, "food", "settings", "protocol", {"interval_hours": 4.5}, at=NOW)
    assert coach.due(profile, NOW + timedelta(hours=4, minutes=30, seconds=10))


def test_snooze_replay_does_not_change_target_or_touch_new_meal():
    store, profile, coach = setup_meal()
    at = NOW + timedelta(hours=3, minutes=30)
    deliver(store, profile, coach.due(profile, at)[0], at)
    first = coach.handle(profile, "/позже 30", source_key="s", now=at)
    coach.handle(
        profile, "/ел обед суп", source_key="lunch", now=at + timedelta(minutes=10)
    )
    replay = FoodCoach(store, Brain()).handle(
        profile, "/позже 30", source_key="s", now=at + timedelta(hours=2)
    )
    assert replay == first
    assert len(store.list(profile, "food", "control")) == 1


def test_breakfast_three_and_profile_isolation():
    store, profile, other = MemoryStore(), uuid4(), uuid4()
    coach = FoodCoach(store, Brain())
    for index in range(3):
        at = NOW + timedelta(minutes=30 * index, seconds=index)
        # Use last successful send, including its jitter, for the next spacing.
        if index == 2:
            at += timedelta(seconds=1)
        notice = coach.due(profile, at)[0]
        assert f"{index + 1}/3" in notice.text
        deliver(store, profile, notice, at)
    assert not coach.due(profile, NOW + timedelta(minutes=95))
    assert "1/3" in coach.due(other, NOW + timedelta(minutes=95))[0].text
