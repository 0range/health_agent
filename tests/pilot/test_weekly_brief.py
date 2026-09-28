from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from test_training import MemoryStore

from health_agent.pilot.health_insights import build_insights
from health_agent.pilot.health_insights_report import HealthInsights
from health_agent.pilot.weekly_brief import WeeklyBrief, training_mix
from health_agent.pilot.weekly_cycle import WeeklyCycle

NOW = datetime(2026, 10, 4, 15, tzinfo=UTC)  # Sunday 18:00 Moscow.


def setup():
    s, p = MemoryStore(), uuid4()
    s.put(p, "shared", "settings", "coaching-cycle", {
        "enabled": True, "health_insights_enabled": True, "weekly_brief_enabled": True,
        "training_sessions": 2,
    }, at=NOW-timedelta(days=30))
    s.put(p, "shared", "goal", "fat", {
        "title": "Reduce fat while preserving strength", "brief_title": "снижение жира",
        "priority": "primary", "status": "active", "period_start": "2026-09-01",
        "target": {"metric": "fat_mass_loss_kg", "minimum": 2, "maximum": 3},
    }, at=NOW-timedelta(days=30))
    s.put(p, "shared", "goal", "training", {
        "title": "Strength and padel", "brief_title": "силовые и падел",
        "priority": "primary", "status": "planned", "period_start": "2026-10-01",
        "period_end": "2026-11-30", "weekly_targets": {"strength": 2, "padel": 1},
    }, at=NOW-timedelta(days=30))
    source = lambda *args: {"status": "ok", "sync_at": NOW.isoformat(), "records": []}
    insights = HealthInsights(s, source)
    return s, p, insights, WeeklyBrief(s, insights)


def test_sunday_morning_weight_is_included_without_inventing_a_complete_food_day():
    s, p, _, _ = setup()
    morning = NOW.replace(hour=5)
    s.put(p, "shared", "weight", "sunday", {"weight_kg": 75}, at=morning)
    s.put(p, "shared", "weight", "future", {"weight_kg": 90}, at=NOW+timedelta(hours=1))
    for category in ("breakfast", "lunch", "afternoon", "dinner"):
        s.put(p, "food", "meal", category, {
            "occurred_at": morning.isoformat(), "category": category,
            "analysis": {"kcal": 300, "protein_g": 20},
        }, at=morning)
    r = build_insights(s, p, NOW, {}, through_date=date(2026, 10, 4))
    assert r["through_date"] == "2026-10-04" and r["recent_from"] == "2026-09-28"
    assert r["weight"]["recent"] == 75 and r["weight"]["recent_days"] == 1
    assert r["food"]["entries"] == 4
    assert r["food"]["calories"]["recent"] is None
    assert r["partial_day"] == "2026-10-04"
    default = build_insights(s, p, NOW, {})
    assert default["through_date"] == "2026-10-03"
    with pytest.raises(ValueError):
        build_insights(s, p, NOW, {}, through_date=date(2026, 10, 5))


def test_calendar_anchor_is_fixed_during_catchup_and_replay():
    s, p, _, brief = setup()
    next_day = NOW + timedelta(days=1)
    text = brief.report(p, next_day, date(2026, 10, 4), "weekly")
    assert "28.09–04.10" in text
    saved = s.by_source(p, "shared", "weekly_brief", "weekly")
    assert saved.payload["evidence"]["through_date"] == "2026-10-04"
    assert brief.report(p, next_day+timedelta(days=1), date(2026, 10, 4), "weekly") == text


def test_short_goal_aware_report_contains_one_suggestion_and_no_long_proposal():
    s, p, _, brief = setup()
    text = brief.report(p, NOW, NOW.date(), "weekly")
    assert len(text) <= 1100
    assert text.count("➡️") == 1
    assert "2 силовые" in text and "1 падел" in text
    assert "подтвержд" in text and "пропустил" not in text
    assert "Черновик общей недели" not in text
    assert not s.list(p, "shared", "cycle_plan")


def test_goal_and_evidence_snapshot_do_not_change_a_frozen_brief():
    s, p, _, brief = setup()
    original = brief.report(p, NOW, NOW.date(), "frozen")
    goal = s.by_source(p, "shared", "goal", "training")
    s.patch(p, goal.id, {**goal.payload, "weekly_targets": {"strength": 1}})
    assert brief.report(p, NOW+timedelta(hours=1), NOW.date(), "frozen") == original
    other = brief.report(uuid4(), NOW, NOW.date(), "frozen")
    assert "2 силовые" not in other


def test_confirmed_training_rhythm_leads_to_body_goal_action_instead_of_repeating_schedule():
    s, p, _, brief = setup()
    for ago, sport in ((1, "Strength"), (3, "Strength"), (2, "Padel")):
        s.put(p, "training", "activity", str(ago), {"sport": sport}, at=NOW-timedelta(days=ago))
    brief.report(p, NOW, NOW.date(), "done")
    row = s.by_source(p, "shared", "weekly_brief", "done")
    assert row.payload["action"]["kind"] == "weigh"


def test_typical_complete_report_is_short_and_does_not_call_observed_weight_loss_fat_loss():
    s, p, _, brief = setup()
    for ago in (1, 3, 8, 10):
        s.put(p, "shared", "weight", str(ago), {"weight_kg": 75 if ago < 7 else 76}, at=NOW-timedelta(days=ago))
        at = NOW-timedelta(days=ago)
        s.put(p, "shared", "body_measurement", str(ago), {
            "body_fat_percent": 20 if ago < 7 else 21,
            "measurement_date": at.date().isoformat(), "recorded_at": at.isoformat()}, at=at)
    for ago in (1, 2, 3):
        for category in ("breakfast", "lunch", "afternoon", "dinner"):
            s.put(p, "food", "meal", f"{ago}:{category}", {
                "occurred_at": (NOW-timedelta(days=ago)).isoformat(), "category": category,
                "analysis": {"kcal": 400, "protein_g": 20}}, at=NOW-timedelta(days=ago))
    text = brief.report(p, NOW, NOW.date(), "complete")
    assert len(text) <= 1100 and "Потерю жира это не подтверждает" in text
    assert "-1 п.п." in text and "оценка прибора" in text


def test_workout_types_deduplicate_by_day_and_do_not_infer_padel_from_generic_sport():
    s, p, _, _ = setup()
    at = NOW-timedelta(days=2)
    for key, sport in (("a", "Strength"), ("b", "Strength"), ("c", "Tennis"), ("d", "Open Water Swim")):
        s.put(p, "training", "activity", key, {"id": key, "sport": sport}, at=at)
    s.put(p, "training", "manual_activity", "m", {"date": at.date().isoformat(), "text": "силовая"}, at=at)
    s.put(p, "training", "manual_activity", "p", {"date": (at-timedelta(days=1)).date().isoformat(), "text": "сыграл в падел"}, at=at)
    s.put(p, "training", "manual_activity", "no", {"date": NOW.date().isoformat(), "text": "не смог сыграть в падел"}, at=NOW)
    s.put(uuid4(), "training", "activity", "other", {"sport": "Padel"}, at=at)
    result = training_mix(s, p, date(2026, 9, 28), date(2026, 10, 4), NOW)
    assert result["strength_days"] == result["padel_days"] == 1
    assert result["other_days"] == 1
    assert result["unknown_manual"] == 1


def test_goals_at_the_next_week_transition_are_used_for_advice_not_past_failure():
    s, p, _, brief = setup()
    september = NOW-timedelta(days=7)
    text = brief.report(p, september, september.date(), "september")
    record = s.by_source(p, "shared", "weekly_brief", "september")
    assert record.payload["current_targets"] == {}
    assert record.payload["next_targets"] == {"strength": 2, "padel": 1}
    assert record.payload["action"]["kind"] == "training_rhythm"
    assert "не выполн" not in text


def test_existing_schedule_key_and_receipt_work_without_accepted_plan():
    s, p, insights, _ = setup()
    cycle = WeeklyCycle(s, insights=insights)
    notices = cycle.due(p, NOW)
    assert len(notices) == 1 and notices[0].key == "cycle:weekly:2026-10-04"
    assert "Черновик" not in notices[0].text
    assert len(notices[0].text) <= 1100
    s.put(p, "sleep", "notice", notices[0].key, {}, at=NOW)
    assert cycle.due(p, NOW+timedelta(minutes=10)) == []
    assert not s.list(p, "shared", "cycle_plan")


def test_main_week_command_and_next_proposal_use_goal_mix_not_old_two_workouts():
    s, p, insights, _ = setup()
    cycle = WeeklyCycle(s, insights=insights)
    assert "Неделя" in cycle.handle(p, "/неделя", "request", NOW, "sleep")
    proposal = cycle.proposal(p, date(2026, 10, 5), NOW)
    assert proposal.payload["training_sessions"] == 3
    assert proposal.payload["training_targets"] == {"strength": 2, "padel": 1}
    assert "2 силовые" in cycle.render(proposal) and "1 падел" in cycle.render(proposal)


def test_fresh_goal_review_can_refresh_proposal_but_never_an_accepted_plan():
    s, p, insights, _ = setup()
    cycle = WeeklyCycle(s, insights=insights)
    first = date(2026, 10, 5)
    proposal = cycle.proposal(p, first, NOW)
    goal = s.by_source(p, "shared", "goal", "training")
    s.patch(p, goal.id, {**goal.payload, "weekly_targets": {"strength": 1, "padel": 1}})
    updated = cycle.proposal(p, first, NOW)
    assert updated.id == proposal.id and updated.payload["revision"] == 2
    assert updated.payload["training_sessions"] == 2
    cycle.handle(p, "Принять неделю 05.10.2026", "accept", NOW, "sleep")
    s.patch(p, goal.id, {**goal.payload, "weekly_targets": {"strength": 3, "padel": 1}})
    assert s.by_source(p, "shared", "cycle_plan", first.isoformat()).payload["training_sessions"] == 2
