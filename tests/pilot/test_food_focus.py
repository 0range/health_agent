from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_food import Brain, MemoryStore

from health_agent.pilot.food import FoodCoach

NOW = datetime(2026, 8, 3, 10, tzinfo=UTC)


def test_focus_is_explicit_persistent_profile_scoped_and_does_not_mutate_food():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    coach.handle(profile, '/фокус Полдник без готовки', source_key='focus', now=NOW)
    again = FoodCoach(store, Brain())
    assert 'Полдник без готовки' in again.handle(profile, '/план', source_key='plan', now=NOW)
    again.handle(profile, '/фокус итог Не было времени', source_key='result', now=NOW)
    weekly = again.handle(profile, '/неделя', source_key='weekly', now=NOW)
    assert 'Не было времени' in weekly
    assert 'Полдник без готовки' in weekly
    assert not store.list(profile, 'food', 'meal')
    assert 'Полдник без готовки' not in again.handle(uuid4(), '/фокус', source_key='other', now=NOW)


def test_proposed_focus_needs_acceptance_and_replay_cannot_reactivate_old_focus():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    proposal = coach.handle(profile, '/фокус', source_key='propose', now=NOW)
    assert '/фокус принять' in proposal
    assert not store.list(profile, 'food', 'focus')
    accepted = coach.handle(profile, '/фокус принять', source_key='accept', now=NOW + timedelta(seconds=1))
    assert len(store.list(profile, 'food', 'focus')) == 1
    coach.handle(profile, '/фокус стоп', source_key='stop', now=NOW + timedelta(days=1))
    assert coach.handle(profile, '/фокус принять', source_key='accept', now=NOW + timedelta(days=2)) == accepted
    plan = coach.handle(profile, '/план', source_key='later-plan', now=NOW + timedelta(days=2))
    assert 'Согласованный фокус' not in plan


def test_focus_expires_after_seven_local_dates_and_custom_result_is_not_invented():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    late = NOW.replace(hour=20, minute=59)
    coach.handle(profile, '/фокус Удобный ужин', source_key='set', now=late)
    still = late + timedelta(days=6)
    assert 'Согласованный фокус' in coach.handle(profile, '/план', source_key='day7', now=still)
    assert 'Согласованный фокус' not in coach.handle(profile, '/план', source_key='day8', now=still + timedelta(minutes=2))
    weekly = coach.handle(profile, '/неделя', source_key='week', now=still)
    assert 'Удобный ужин' in weekly
    assert 'неизвест' in weekly.lower() or 'пока нет' in weekly.lower()


def test_weekly_proposal_can_be_accepted_but_never_accepts_itself():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    weekly = coach.handle(profile, '/неделя', source_key='week', now=NOW)
    assert 'Предлагаю фокус' in weekly and '/фокус принять' in weekly
    assert not store.list(profile, 'food', 'focus')
    coach.handle(profile, '/фокус принять', source_key='accept', now=NOW+timedelta(minutes=1))
    assert len(store.list(profile, 'food', 'focus')) == 1


def test_expired_proposal_is_not_accepted_and_missing_outcome_is_not_failure():
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    coach.handle(profile, '/фокус', source_key='propose', now=NOW)
    reply = coach.handle(profile, '/фокус принять', source_key='late', now=NOW+timedelta(days=8))
    assert 'Нет свежего' in reply
    assert not store.list(profile, 'food', 'focus')
