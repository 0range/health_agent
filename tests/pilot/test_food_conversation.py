import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from test_food import Brain, MemoryStore

from health_agent.pilot.contracts import Attachment
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_history import build_food_history

NOW = datetime(2026, 8, 3, 10, tzinfo=UTC)


def setup():
    store, profile = MemoryStore(), uuid4()
    brain = Brain(json.dumps({'foods': ['рис', 'рыба'], 'kcal': 400}))
    coach = FoodCoach(store, brain)
    store.put(profile, 'food', 'settings', 'protocol', {'meal_assessment_version': 1})
    coach.handle(profile, '/ел обед рис и рыба', source_key='lunch', now=NOW)
    return store, profile, brain, coach


def test_reminder_response_is_next_meal_not_revision_and_ack_is_not_a_meal():
    store, profile, brain, coach = setup()
    original = store.list(profile, 'food', 'meal')[0]
    at = NOW + timedelta(hours=4)
    notice = coach.due(profile, at)[0]
    store.put(profile, 'food', 'notice', notice.key, {'delivered_at': at.isoformat()}, at=at)
    brain.reply = json.dumps({'foods': ['удон', 'овощи'], 'kcal': 500})
    reply = coach.handle(profile, 'удон с овощами', source_key='snack', now=at + timedelta(minutes=20))
    meals = store.list(profile, 'food', 'meal')
    assert len(meals) == 2
    assert meals[0].payload['category'] == 'afternoon'
    assert store.get(profile, original.id).payload == original.payload
    assert 'полдник' in reply.lower()
    coach.handle(profile, 'я же уже поел', source_key='ack', now=at + timedelta(minutes=21))
    assert len(store.list(profile, 'food', 'meal')) == 2
    assert not coach.due(profile, at + timedelta(minutes=51))


def test_ack_closes_only_current_series_and_replay_does_not_close_new_one():
    store, profile, _, coach = setup()
    at = NOW + timedelta(hours=3, minutes=30)
    notice = coach.due(profile, at)[0]
    store.put(profile, 'food', 'notice', notice.key, {'delivered_at': at.isoformat()}, at=at)
    reply = coach.handle(profile, 'Уже ел', source_key='ack', now=at)
    assert len(store.list(profile, 'food', 'meal')) == 1
    assert not coach.due(profile, at + timedelta(minutes=31))
    next_day = NOW + timedelta(days=1)
    coach.handle(profile, '/ел обед суп', source_key='tomorrow', now=next_day)
    assert FoodCoach(store, Brain()).handle(profile, 'Уже ел', source_key='ack', now=next_day) == reply
    assert coach.due(profile, next_day + timedelta(hours=3, minutes=30))


def test_ambiguous_late_text_does_not_overwrite_existing_meal():
    store, profile, _, coach = setup()
    before = store.list(profile, 'food', 'meal')[0]
    reply = coach.handle(profile, 'Что-то другое', source_key='ambiguous', now=NOW + timedelta(hours=3))
    assert 'новый' in reply.lower()
    assert store.get(profile, before.id).payload == before.payload
    assert not store.list(profile, 'food', 'comment')


def test_mixed_additions_and_user_calories_preserve_food_and_provenance():
    store, profile, _, coach = setup()
    coach.handle(profile, 'Так же яблоко, бутерброд и печеньку. Думаю это не 400 ккал', source_key='add', now=NOW + timedelta(minutes=2))
    meal = store.list(profile, 'food', 'meal')[0]
    foods = meal.payload['analysis']['foods']
    assert {'рис', 'рыба'} <= set(foods)
    assert any('яблок' in f for f in foods)
    assert any('бутерброд' in f for f in foods)
    reply = coach.handle(profile, 'Я бы оценил в 750', source_key='calories', now=NOW + timedelta(minutes=3))
    assert '750' in reply and 'тво' in reply.lower()
    assert store.list(profile, 'food', 'meal')[0].payload['user_kcal']['value'] == 750
    assert '750' in coach.handle(profile, '/сегодня', source_key='today', now=NOW + timedelta(minutes=4))


def test_help_never_becomes_food_or_updates_existing_meal():
    store, profile, _, coach = setup()
    before = store.list(profile, 'food', 'meal')[0]
    for i, text in enumerate(['Голодный', 'Не успеваю пообедать', 'Что выбрать на ужин?']):
        reply = coach.handle(profile, text, source_key=f'help-{i}', now=NOW + timedelta(minutes=10+i))
        assert 'основной Health Agent' not in reply
        assert 'новый приём' not in reply
    assert store.list(profile, 'food', 'meal') == [before]
    assert not store.list(profile, 'food', 'comment')


def test_photo_uses_open_reminder_category_even_after_dinner_clock_hour(tmp_path):
    store, profile, _, coach = setup()
    at = NOW + timedelta(hours=5)  # 18:00 local, but still answering the afternoon reminder.
    sent = NOW + timedelta(hours=3, minutes=30)
    notice = coach.due(profile, sent)[0]
    store.put(profile, 'food', 'notice', notice.key, {'delivered_at': sent.isoformat()}, at=sent)
    coach.handle(profile, '', source_key='photo', now=at, attachment=Attachment(tmp_path/'plate.jpg', 'image/jpeg'))
    assert store.list(profile, 'food', 'meal')[0].payload['category'] == 'afternoon'


def test_question_does_not_rewrite_plate_and_user_estimate_expires_with_new_food():
    store, profile, brain, coach = setup()
    calls = len(brain.calls)
    coach.handle(profile, 'Почему здесь мало калорий?', source_key='question', now=NOW)
    assert len(brain.calls) == calls
    coach.handle(profile, 'Я бы оценил в 700', source_key='estimate', now=NOW)
    meal = store.list(profile, 'food', 'meal')[0]
    assert meal.payload['analysis']['kcal'] == 400
    assert build_food_history(store, profile, NOW)['meals'][0]['kcal_source'] == 'user_estimate'
    coach.handle(profile, 'Ещё хлеб', source_key='bread', now=NOW+timedelta(minutes=1))
    meal = store.list(profile, 'food', 'meal')[0]
    assert 'user_kcal' not in meal.payload
    assert meal.payload['user_kcal_history'][0]['value'] == 700


def test_ambiguous_text_can_be_explicitly_bound_to_original_meal():
    store, profile, _, coach = setup()
    first = store.list(profile, 'food', 'meal')[0]
    coach.handle(profile, 'Миска побольше', source_key='ambiguous', now=NOW+timedelta(hours=2))
    coach.handle(profile, 'тот же', source_key='confirm', now=NOW+timedelta(hours=2, minutes=1))
    assert len(store.list(profile, 'food', 'meal')) == 1
    assert store.list(profile, 'food', 'comment')[0].payload['meal_id'] == first.id


def test_superseded_false_meal_stays_auditable_but_is_not_counted():
    store, profile, _, coach = setup()
    first = store.list(profile, 'food', 'meal')[0]
    false = store.put(profile, 'food', 'meal', 'old-error', {
        **first.payload, 'superseded_by_meal':first.id, 'occurred_at':(NOW+timedelta(minutes=1)).isoformat(),
    }, at=NOW+timedelta(minutes=1))
    history = build_food_history(store, profile, NOW+timedelta(minutes=2))
    assert history['recorded_meal_count'] == 1
    assert store.get(profile, false.id) is not None
    assert coach._latest_meal(profile).id == first.id
