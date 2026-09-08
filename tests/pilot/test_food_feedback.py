"""Regressions for the first real breakfast and immediate meal timing."""
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach


@pytest.mark.parametrize('milk', ['кокосовое молоко', 'миндальное молоко', 'овсяное молоко', 'coconut milk', 'йогурт на кокосовом молоке'])
def test_plant_milk_is_not_dairy_even_if_model_labels_it(milk):
    assert 'dairy' not in FoodCoach._components({'foods': [milk, 'яблоко'], 'plate_components': ['dairy', 'fruit']})
    assert 'dairy' in FoodCoach._components({'foods': [milk, 'творог'], 'plate_components': ['dairy']})


def test_calories_and_next_meal_match_reminder_and_survive_comment(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    brain = Brain(json.dumps({'foods': ['овсянка', 'кокосовое молоко', 'яблоко'], 'kcal': 350, 'portion_estimate': 'миска'}))
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 8, 5, 23, tzinfo=UTC)
    photo = Attachment(tmp_path / 'meal.jpg', 'image/jpeg', '')
    reply = coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert '350 ккал' in reply and 'примерно' in reply.lower()
    assert '12:13' in reply and 'молочный продукт' not in reply
    assert not coach.due(profile, now + timedelta(hours=3, minutes=49))
    assert coach.due(profile, now + timedelta(hours=3, minutes=50))
    assert '12:13' in coach.handle(profile, 'там ещё миндаль', source_key='comment', now=now + timedelta(minutes=5))
    assert len(store.list(profile, 'food', 'meal')) == 1
    assert '12:33' in coach.handle(profile, '', source_key='photo2', now=now + timedelta(minutes=20), attachment=photo)


def test_analysis_outage_keeps_time_and_pause_does_not_promise_delivery(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain(RuntimeError('offline')))
    now = datetime(2026, 9, 8, 5, 23, tzinfo=UTC)
    photo = Attachment(tmp_path / 'meal.jpg', 'image/jpeg', '')
    reply = coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert '12:13' in reply and 'сохранён' in reply
    coach.handle(profile, '/напоминания выкл', source_key='off', now=now)
    reply = coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert '12:13' in reply and 'выключены' in reply
    assert 'продолжит работать' not in reply


def test_correction_replaces_wrong_food_and_preserves_original_analysis(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    brain = Brain(json.dumps({'foods': ['молоко'], 'plate_components': ['dairy'], 'kcal': 350}))
    coach = FoodCoach(store, brain)
    now = datetime(2026, 9, 8, 5, tzinfo=UTC)
    coach.handle(profile, '', source_key='photo', now=now, attachment=Attachment(tmp_path / 'meal.jpg', 'image/jpeg', ''))
    brain.reply = json.dumps({'foods': ['кокосовое молоко'], 'plate_components': [], 'kcal': 300})
    reply = coach.handle(profile, 'это кокосовое молоко', source_key='comment', now=now + timedelta(minutes=1))
    assert 'молочный продукт' not in reply and '300 ккал' in reply
    meal = store.list(profile, 'food', 'meal')[0]
    assert meal.payload['photos'][0]['analysis']['foods'] == ['молоко']
    assert meal.payload['analysis']['foods'] == ['кокосовое молоко']


def test_dinner_does_not_suggest_night_meal(tmp_path: Path):
    coach, profile = FoodCoach(MemoryStore(), Brain()), uuid4()
    reply = coach.handle(profile, 'ужин', source_key='dinner', now=datetime(2026, 9, 8, 17, tzinfo=UTC), attachment=Attachment(tmp_path / 'meal.jpg', 'image/jpeg', ''))
    assert 'завтрак' in reply.lower() and '23:50' not in reply


@pytest.mark.parametrize('food', ['овсяная каша с молоком', 'йогурт с кокосом', 'рисовая каша на молоке'])
def test_grains_and_coconut_flavour_do_not_hide_real_dairy(food):
    assert 'dairy' in FoodCoach._components({'foods': [food]})


def test_configured_interval_snooze_and_skip_are_reflected_in_photo_reply(tmp_path: Path):
    store, profile = MemoryStore(), uuid4()
    coach = FoodCoach(store, Brain())
    store.put(profile, 'food', 'settings', 'protocol', {'interval_hours': 3})
    now = datetime(2026, 9, 8, 5, tzinfo=UTC)
    photo = Attachment(tmp_path / 'meal.jpg', 'image/jpeg', '')
    reply = coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert '11:20' in reply
    coach.handle(profile, '/позже 30', source_key='snooze', now=now + timedelta(hours=3, minutes=20))
    reply = coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert '11:50' in reply
    assert coach.due(profile, now + timedelta(hours=3, minutes=50))
    coach.handle(profile, '/пропустить', source_key='skip', now=now + timedelta(hours=3, minutes=51))
    assert 'Интервал пропущен' in coach.handle(profile, '', source_key='photo', now=now, attachment=photo)
    assert not coach.due(profile, now + timedelta(hours=4))
