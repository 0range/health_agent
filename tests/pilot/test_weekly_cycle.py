from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from test_training import MemoryStore

from health_agent.pilot.food_focus import FoodFocus
from health_agent.pilot.weekly_cycle import WeeklyCycle, context, enabled, keyboard
from health_agent.pilot.weekly_evidence import evidence

NOW = datetime(2026, 8, 7, 15, tzinfo=UTC)  # Friday; next Monday Aug 10.


def setup():
    store, profile = MemoryStore(), uuid4()
    store.put(
        profile,
        "shared",
        "settings",
        "coaching-cycle",
        {"enabled": True, "training_sessions": 2},
        at=NOW,
    )
    store.put(
        profile,
        "shared",
        "goal",
        "current",
        {
            "title": "Current goal",
            "status": "active",
            "priority": "primary",
            "period_start": "2026-08-01",
            "period_end": "2026-08-31",
        },
        at=NOW,
    )
    store.put(
        profile,
        "shared",
        "goal",
        "future",
        {
            "title": "Future goal",
            "status": "planned",
            "priority": "primary",
            "period_start": "2026-09-01",
            "period_end": "2026-09-30",
        },
        at=NOW,
    )
    return store, profile, WeeklyCycle(store)


def accepted():
    store, profile, cycle = setup()
    cycle.handle(profile, "/цикл", "show", NOW, "sleep")
    reply = cycle.handle(profile, "Принять неделю 10.08.2026", "accept", NOW, "sleep")
    assert "принята" in reply
    return store, profile, cycle


def test_proposal_is_not_plan_and_acceptance_mirrors_exact_dates_with_replay():
    store, profile, cycle = setup()
    reply = cycle.handle(profile, "/цикл", "show", NOW, "sleep")
    assert "Current goal" in reply and "Future goal" not in reply
    assert not store.list(profile, "shared", "cycle_plan") and not store.list(
        profile, "food", "focus"
    )
    assert keyboard(reply)["keyboard"][0] == ["Принять неделю 10.08.2026"]
    accepted_reply = cycle.handle(
        profile, "Принять неделю 10.08.2026", "accept", NOW, "sleep"
    )
    plan = store.list(profile, "shared", "cycle_plan")[0]
    assert (
        plan.payload["from_date"] == "2026-08-10"
        and plan.payload["through_date"] == "2026-08-16"
    )
    assert plan.payload["training_sessions"] == 2
    assert len(store.list(profile, "training", "accepted_plan")) == 1
    assert (
        WeeklyCycle(store).handle(
            profile,
            "Принять неделю 10.08.2026",
            "accept",
            NOW + timedelta(days=20),
            "sleep",
        )
        == accepted_reply
    )
    assert len(store.list(profile, "shared", "cycle_plan")) == 1
    assert "Current goal" in str(context(store, profile, NOW))


def test_food_focus_keeps_current_until_accepted_future_week_starts():
    store, profile, cycle = setup()
    f = FoodFocus(store)
    f.handle(profile, "/фокус Current task", "old", NOW, {})
    cycle.handle(profile, "/цикл", "show", NOW, "sleep")
    cycle.handle(profile, "Принять неделю 10.08.2026", "accept", NOW, "sleep")
    assert f.current(profile, NOW).payload["text"] == "Current task"
    assert f.current(profile, NOW + timedelta(days=4)).payload["cycle_plan_id"]


def test_old_button_unknown_profile_invalid_date_and_expired_proposal_cannot_accept():
    store, profile, cycle = setup()
    assert cycle.handle(uuid4(), "/цикл", "foreign", NOW, "sleep") is None
    assert "недоступно" in cycle.handle(
        profile, "Принять неделю 03.08.2026", "old", NOW, "sleep"
    )
    assert not store.list(profile, "shared", "cycle_plan")
    assert cycle.handle(profile, "Принять неделю 99.99.2026", "invalid", NOW, "sleep")
    cycle.handle(profile, "/цикл", "show", NOW, "sleep")
    cycle.handle(
        profile, "Принять неделю 10.08.2026", "late", NOW + timedelta(days=20), "sleep"
    )
    assert not store.list(profile, "shared", "cycle_plan")


def test_simplification_requires_acceptance_and_retry_finishes_mirror():
    store, profile, cycle = setup()
    cycle.handle(profile, "/цикл", "show", NOW, "sleep")
    cycle.handle(profile, "Упростить неделю 10.08.2026", "simple", NOW, "sleep")
    assert not store.list(profile, "shared", "cycle_plan")
    proposal = store.list(profile, "shared", "cycle_proposal")[0]
    assert proposal.payload["training_sessions"] == 1
    store.put(
        profile,
        "shared",
        "cycle_plan",
        "2026-08-10",
        {**proposal.payload, "status": "accepted"},
        at=NOW,
    )
    cycle.handle(profile, "Принять неделю 10.08.2026", "retry", NOW, "sleep")
    assert len(store.list(profile, "training", "accepted_plan")) == 1
    assert len(store.list(profile, "food", "focus")) == 1


def test_travel_plan_is_scoped_and_uses_relevant_nights():
    store, profile, cycle = setup()
    store.put(
        profile,
        "shared",
        "sleep_context",
        "trip",
        {
            "source": "user",
            "timezone": "Europe/Moscow",
            "night_start_date": "2026-08-10",
            "wake_date": "2026-08-11",
            "location": "away",
            "evidence": "planned",
        },
    )
    proposal = cycle.proposal(profile, date(2026, 8, 10), NOW)
    assert "поездке" in proposal.payload["food_focus"]
    assert proposal.payload["away_nights"][0]["evidence"] == "planned"


def test_due_welcome_weekly_catchup_midweek_and_quiet_hours_are_bounded():
    store, profile, cycle = setup()
    assert cycle.due(profile, NOW.replace(hour=21)) == []
    notice = cycle.due(profile, NOW)[0]
    assert notice.key == "cycle:welcome"
    store.put(profile, "sleep", "notice", notice.key, {}, at=NOW)
    assert cycle.due(profile, NOW) == []
    cycle.handle(profile, "Принять неделю 10.08.2026", "accept", NOW, "sleep")
    wed = datetime(2026, 8, 12, 15, tzinfo=UTC)
    check = cycle.due(profile, wed)[0]
    assert check.key.startswith("cycle:checkin:")
    store.put(profile, "sleep", "notice", check.key, {}, at=wed)
    assert not cycle.due(profile, wed + timedelta(minutes=10))
    # A missed Sunday is caught up Monday; training and food share one notice.
    monday = datetime(2026, 8, 17, 8, tzinfo=UTC)
    review = cycle.due(profile, monday)[0]
    assert "Итог общей недели" in review.text and "2026-08-17" in review.text
    store.put(profile, "sleep", "notice", review.key, {}, at=monday)
    assert cycle.due(profile, monday) == []


def test_evidence_counts_facts_with_no_cross_profile_or_coros_manual_double_count():
    store, profile, _ = accepted()
    at = datetime(2026, 8, 12, 8, tzinfo=UTC)
    for key in ["a", "duplicate"]:
        store.put(profile, "training", "activity", key, {"id": "same"}, at=at)
    store.put(uuid4(), "training", "activity", "foreign", {"id": "other"}, at=at)
    store.put(
        profile, "training", "manual_activity", "m", {"date": "2026-08-12"}, at=at
    )
    store.put(
        profile, "training", "manual_activity", "m2", {"date": "2026-08-11"}, at=at
    )
    store.put(
        profile,
        "shared",
        "weight",
        "old",
        {"weight_kg": 80, "source": "user_report"},
        at=NOW - timedelta(days=20),
    )
    facts = evidence(store, profile, date(2026, 8, 10), date(2026, 8, 16), at)
    assert facts["training_count"] == 2 and facts["possible_manual_overlap"]
    assert (
        facts["weight_stale"]
        and facts["meal_count"] == 0
        and facts["rested_mean"] is None
    )


def test_self_reports_are_durable_not_food_and_feedback_informs_next_proposal():
    store, profile, cycle = accepted()
    at = datetime(2026, 8, 12, 15, tzinfo=UTC)
    for text, key in [
        ("/вес 79,5", "w"),
        ("/тренировка 2026-08-11 бег", "t"),
        ("/цикл итог Не успеваю, нет времени", "r"),
    ]:
        reply = cycle.handle(profile, text, key, at, "food")
        assert (
            reply
            and cycle.handle(profile, text, key, at + timedelta(days=1), "food")
            == reply
        )
    assert len(store.list(profile, "shared", "weight")) == 1
    assert not store.list(profile, "food", "meal")
    assert len(store.list(profile, "training", "manual_activity")) == 1
    next_plan = cycle.proposal(profile, date(2026, 8, 17), at)
    assert next_plan.payload["previous_feedback"] == "Не успеваю, нет времени"
    assert next_plan.payload["training_sessions"] == 1
    assert not store.by_source(profile, "shared", "cycle_plan", "2026-08-17")


def test_stop_replay_stays_stopped_and_does_not_restore_legacy_weeklies():
    from health_agent.pilot.weekly_cycle import owns_weeklies

    store, profile, cycle = accepted()
    reply = cycle.handle(profile, "/цикл стоп", "stop", NOW, "sleep")
    assert not enabled(store, profile) and cycle.due(profile, NOW) == []
    assert owns_weeklies(store, profile)
    assert (
        cycle.handle(profile, "/цикл стоп", "stop", NOW + timedelta(days=1), "sleep")
        == reply
    )
