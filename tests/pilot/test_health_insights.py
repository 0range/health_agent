from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest
from test_training import MemoryStore

from health_agent.pilot.health_insights import build_insights
from health_agent.pilot.health_insights_report import HealthInsights, render
from health_agent.pilot.weekly_cycle import WeeklyCycle

NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)


def whoop():
    return {"status": "ok", "sync_at": NOW.isoformat(), "records": []}


def meal(store, profile, at, category, kcal=300, protein=20, **extra):
    return store.put(profile, "food", "meal", str(uuid4()), {
        "category": category, "occurred_at": at.isoformat(),
        "analysis": {"foods": ["еда"], "kcal": kcal, "protein_g": protein}, **extra,
    }, at=at)


def sync(store, profile, at=NOW):
    store.put(profile, "training", "sync_run", str(uuid4()), {
        "status": "success", "since": "2026-09-01", "until": "2026-09-28",
        "finished_at": at.isoformat(),
    }, at=at)


def sleep(wake, hours=8, **extra):
    end = datetime.combine(wake, datetime.min.time(), UTC) + timedelta(hours=4)
    return {"start_at": (end - timedelta(hours=hours)).isoformat(), "end_at": end.isoformat(),
            "sleep_hours": hours, "hrv_ms": 50, "resting_hr": 55, "recovery": 75, **extra}


def test_weight_daily_medians_not_repeat_measurements_and_undated_whoop_is_not_a_trend():
    store, p = MemoryStore(), uuid4()
    for ago, kg in [(1, 75), (2, 75), (3, 75), (8, 76), (9, 76), (10, 76)]:
        at = NOW - timedelta(days=ago)
        for source in ("apple", "copy"):
            store.put(p, "shared", "weight", f"{ago}:{source}", {"weight_kg": kg}, at=at)
    source = {**whoop(), "body_weight_kg_without_measurement_date": 130}
    r = build_insights(store, p, NOW, source)
    assert r["weight"]["delta_kg"] == -1
    assert r["weight"]["recent_days"] == r["weight"]["previous_days"] == 3
    assert r["weight"]["latest"]["kg"] == 75
    assert "не измеренная потеря жира" in render(r)


def test_two_measurement_days_per_week_allow_a_preliminary_weight_comparison():
    store, p = MemoryStore(), uuid4()
    for ago, kg in [(1, 75), (4, 75), (8, 76), (11, 76)]:
        store.put(p, "shared", "weight", str(ago), {"weight_kg": kg}, at=NOW-timedelta(days=ago))
    r = build_insights(store, p, NOW, whoop())
    assert r["weight"]["delta_kg"] == -1
    assert r["weight"]["recent_days"] == r["weight"]["previous_days"] == 2
    assert "Предварительное сравнение" in render(r)


def test_composition_estimates_are_daily_and_units_and_profile_stay_separate():
    store, p = MemoryStore(), uuid4()
    for ago in (1, 4, 8, 11):
        at = NOW-timedelta(days=ago)
        payload = {"body_fat_percent": 20 if ago < 7 else 21,
                   "muscle_mass_kg": 56 if ago < 7 else None,
                   "muscle_percent": None if ago < 7 else 42,
                   "recorded_at": NOW.isoformat(), "measurement_date": at.date().isoformat()}
        for copy in ("original", "copy"):
            store.put(p, "shared", "body_measurement", f"{ago}:{copy}", payload, at=at)
    store.put(uuid4(), "shared", "body_measurement", "foreign", {
        "body_fat_percent": 99, "recorded_at": NOW.isoformat(), "measurement_date": NOW.date().isoformat()}, at=NOW)
    r = build_insights(store, p, NOW, whoop())
    body = r["body_composition"]
    assert body["body_fat_percent"]["delta"] == -1
    assert body["body_fat_percent"]["recent_days"] == 2
    assert body["muscle_mass_kg"]["previous"] is None
    assert body["muscle_percent"]["recent"] is None
    assert body["latest"]["body_fat_percent"] == 20
    assert "Это оценки весов" in render(r)


def test_todays_composition_is_visible_but_does_not_enter_completed_week_comparison():
    store, p = MemoryStore(), uuid4()
    for key, when in (("today", NOW), ("future", NOW+timedelta(days=1))):
        store.put(p, "shared", "body_measurement", key, {
            "body_fat_percent": 22, "recorded_at": when.isoformat(),
            "measurement_date": when.date().isoformat()}, at=when)
    r = build_insights(store, p, NOW, whoop())
    assert r["body_composition"]["latest"]["date"] == NOW.date().isoformat()
    assert r["body_composition"]["body_fat_percent"]["recent_days"] == 0


def test_sparse_invalid_future_other_profile_weight_and_partial_day_are_excluded():
    store, p = MemoryStore(), uuid4()
    for i, value in enumerate((True, float("nan"), -3, 0, float("inf"))):
        store.put(p, "shared", "weight", str(i), {"weight_kg": value}, at=NOW-timedelta(days=1))
    store.put(p, "shared", "weight", "future", {"weight_kg": 200}, at=NOW+timedelta(days=1))
    store.put(uuid4(), "shared", "weight", "other", {"weight_kg": 200}, at=NOW-timedelta(days=1))
    store.put(p, "shared", "weight", "today", {"weight_kg": 75}, at=NOW)
    r = build_insights(store, p, NOW, whoop())
    assert r["weight"]["delta_kg"] is None and r["weight"]["recent_days"] == 0
    assert r["weight"]["latest"]["kg"] == 75
    assert r["through_date"] == "2026-09-27"


def test_food_estimates_snacks_and_missing_entries_do_not_become_daily_intake_or_deficit():
    store, p = MemoryStore(), uuid4()
    at = NOW - timedelta(days=1)
    for category in ("breakfast", "lunch", "afternoon", "dinner"):
        meal(store, p, at, category)
    meal(store, p, at, "bridge_snack", 70, 3)
    meal(store, p, at-timedelta(days=1), "breakfast", None)
    meal(store, p, at, "lunch", 9999, superseded_by_meal="replacement")
    meal(store, p, NOW, "breakfast", 9999)
    r = build_insights(store, p, NOW, whoop())
    assert r["food"]["entries"] == 6
    assert r["food"]["four_slot_days"] == 1
    assert r["food"]["calories"]["recent"] == 1270
    assert r["food"]["protein"]["recent"] == 83
    assert r["food"]["unknown_calories"] == 1
    assert r["food"]["calories"]["delta"] is None
    assert "дефицит этим не доказан" in render(r)


def test_coros_deduplicates_and_apple_whoop_copies_do_not_inflate_training():
    store, p = MemoryStore(), uuid4()
    at = NOW-timedelta(days=1)
    for key in ("one", "copy"):
        store.put(p, "training", "activity", key, {"id": "same", "duration_s": 1200}, at=at)
    store.put(p, "training", "apple_workout", "apple-copy", {"duration_s": 1200}, at=at)
    store.put(p, "training", "manual_activity", "manual", {"date": "2026-09-27"}, at=at)
    sync(store, p)
    r = build_insights(store, p, NOW, whoop())
    assert r["training"]["sessions"] == 1 and r["training"]["minutes"] == 20
    assert r["training"]["days"] == 1
    assert r["training"]["manual_days_without_coros"] == 0
    assert r["training"]["checked_days"] == 7


def test_a_same_day_sync_does_not_prove_later_hours_have_no_workout():
    store, p = MemoryStore(), uuid4()
    sync(store, p, NOW-timedelta(days=1))
    r = build_insights(store, p, NOW, whoop())
    assert not r["daily"][-1]["coros_day_checked"]
    assert r["training"]["checked_days"] == 6


def test_sleep_uses_wake_day_and_longest_main_sleep_with_valid_values():
    store, p = MemoryStore(), uuid4()
    source = whoop()
    source["records"] = [sleep(date(2026,9,27), 6), sleep(date(2026,9,27), 8),
                         sleep(date(2026,9,29), 9), sleep(date(2026,9,26), 7, hrv_ms=True)]
    r = build_insights(store, p, NOW, source)
    assert r["recovery"]["sleep_hours"]["recent"] == 7.5
    assert r["recovery"]["sleep_hours"]["recent_days"] == 2
    assert r["recovery"]["hrv_ms"]["recent_days"] == 1
    assert not r["recovery"]["sleep_hours"]["enough_for_comparison"]


def association_fixture():
    store, p, source = MemoryStore(), uuid4(), whoop()
    for i in range(12):
        wake = date(2026,9,16)+timedelta(days=i)
        observation = sleep(wake, 7 if i%2 else 8)
        source["records"].append(observation)
        start = datetime.fromisoformat(observation["start_at"])
        meal(store, p, start-timedelta(hours=1 if i%2 else 3), "dinner", time_source="user")
        if i%2:
            at = start-timedelta(hours=4)
            store.put(p,"training","activity",str(i),{"id":str(i),"duration_s":1800},at=at)
    sync(store,p)
    return store,p,source


def test_predeclared_comparisons_show_sample_sizes_and_only_observational_differences():
    store,p,source=association_fixture()
    r=build_insights(store,p,NOW,source)
    dinner=r["associations"][0]
    assert dinner["status"]=="exploratory" and dinner["difference_hours"]==-1
    assert dinner["left_nights"]==dinner["right_nights"]==6
    assert "Совпадение не доказывает влияние" in render(r)
    assert len(render(r)) < 3000


def test_away_nights_and_uncertain_meal_times_do_not_enter_food_comparison():
    store,p,source=association_fixture()
    for i in range(5):
        wake=date(2026,9,16)+timedelta(days=i)
        store.put(p,"shared","sleep_context",str(i),{
            "wake_date":wake.isoformat(),"night_start_date":(wake-timedelta(days=1)).isoformat(),
            "source":"user","timezone":"Europe/Moscow","evidence":"reported","location":"away",
        },at=NOW-timedelta(days=14))
    for row in store.list(p,"food","meal")[:3]:
        store.patch(p,row.id,{**row.payload,"time_source":"message_time"})
    r=build_insights(store,p,NOW,source)
    assert r["associations"][0]["status"]=="insufficient"
    assert r["associations"][0]["away_excluded"]==5
    assert "причину" in render(r)


def test_no_source_sync_cannot_make_nontraining_comparison_group():
    store,p,source=association_fixture()
    store.records=[(owner,r) for owner,r in store.records if r.kind!='sync_run']
    r=build_insights(store,p,NOW,source)
    training=r["associations"][1]
    assert training["right_nights"]==0 and training["status"]=="insufficient"


def test_failure_reports_unavailable_and_replay_does_not_recompute():
    store,p=MemoryStore(),uuid4()
    calls=[]
    def unavailable(*args):
        calls.append(args)
        raise RuntimeError('private credential error')
    insights=HealthInsights(store,unavailable)
    text=insights.report(p,NOW,'request-one')
    assert 'private credential' not in text and 'не подтверждена' in text
    assert insights.report(p,NOW+timedelta(days=1),'request-one')==text
    assert len(calls)==1 and len(store.list(p,'shared','health_insight'))==1


def test_manual_weight_and_on_demand_insights_work_without_an_accepted_or_enabled_plan():
    store,p=MemoryStore(),uuid4()
    cycle=WeeklyCycle(store,insights=HealthInsights(store))
    assert '75.3' in cycle.handle(p,'вес 75,3 кг','weight',NOW,'sleep')
    assert len(store.list(p,'shared','weight'))==1
    assert 'Общий разбор' in cycle.handle(p,'инсайты','insights',NOW,'sleep')
    assert not store.list(p,'shared','cycle_plan')
    assert cycle.due(p,NOW)==[]


def test_weekly_report_exists_without_accepted_plan_and_uses_existing_delivery_key():
    store,p=MemoryStore(),uuid4()
    sunday=NOW-timedelta(days=1)+timedelta(hours=5)
    store.put(p,'shared','settings','coaching-cycle',{'enabled':True,'health_insights_enabled':True},at=NOW-timedelta(days=10))
    cycle=WeeklyCycle(store,insights=HealthInsights(store))
    notices=cycle.due(p,sunday)
    assert len(notices)==1 and notices[0].key=='cycle:weekly:2026-09-27'
    assert 'Общий разбор' in notices[0].text and 'Черновик общей недели' in notices[0].text
    assert len(notices[0].text)<4096
    assert cycle.due(p,sunday+timedelta(minutes=3))[0].text==notices[0].text
    store.put(p,'sleep','notice',notices[0].key,{},at=sunday)
    assert cycle.due(p,sunday+timedelta(minutes=4))==[]


def test_naive_now_rejected():
    with pytest.raises(ValueError,match='timezone'):
        build_insights(MemoryStore(),uuid4(),NOW.replace(tzinfo=None),whoop())


def test_main_context_retains_joint_facts_when_sleep_question_is_focused():
    from test_training import Brain

    from health_agent.pilot.sleep import SleepCoach

    store,p=MemoryStore(),uuid4()
    facts={"weight":{"delta_kg":None},"limits":["association is not causation"]}
    coach=SleepCoach(store,Brain(),health_context=lambda *_:{"joint_health_observations":facts})
    payload=coach._prompt_payload(p,"Как сон связан с тренировками?",False,NOW)
    assert payload["verified_health_context"]["joint_health_observations"]==facts


def test_actions_route_insights_and_weight_without_calling_llm_and_reject_other_profile():
    from types import SimpleNamespace

    from health_agent.pilot.runtime import PilotActions

    class Coach:
        def handle(self,*args,**kwargs):
            raise AssertionError("deterministic action must not call model")
    store,p=MemoryStore(),uuid4()
    actions=PilotActions(Coach(),store,"sleep",p)
    context=SimpleNamespace(profile_id=p,bot_id=1,update_id=1,sent_at=NOW,received_at=NOW)
    reply=actions.handle(context,"инсайты")
    assert 'Общий разбор' in reply
    assert actions.handle(context,"инсайты")==reply
    context.update_id=2
    assert '76.1' in actions.handle(context,'вес 76,1')
    assert len(store.list(p,'shared','weight'))==1
    context.profile_id=uuid4()
    assert 'другого профиля' in actions.handle(context,'инсайты')


def test_frozen_delivery_retries_do_not_send_changed_observations():
    from health_agent.pilot.runtime import dispatch_notices

    store,p=MemoryStore(),uuid4()
    sunday=NOW-timedelta(days=1)+timedelta(hours=5)
    store.put(p,'shared','settings','coaching-cycle',{'enabled':True,'health_insights_enabled':True},at=NOW-timedelta(days=10))
    class Coach:
        def due(self,*args): return []
    class Messenger:
        def __init__(self): self.calls=[]
        def send_to_profile(self,p,text,**kwargs):
            self.calls.append((text,kwargs))
            if len(self.calls)==1: raise RuntimeError('temporary delivery failure')
    messenger=Messenger()
    with pytest.raises(RuntimeError):
        dispatch_notices(Coach(),store,messenger,p,'sleep',sunday)
    store.put(p,'shared','weight','new-weight',{'weight_kg':70},at=sunday-timedelta(hours=1))
    assert dispatch_notices(Coach(),store,messenger,p,'sleep',sunday+timedelta(minutes=1))==1
    assert messenger.calls[0]==messenger.calls[1]
    assert dispatch_notices(Coach(),store,messenger,p,'sleep',sunday+timedelta(minutes=2))==0
