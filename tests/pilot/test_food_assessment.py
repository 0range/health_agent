import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_food import Brain, MemoryStore

from health_agent.pilot.food import FoodCoach
from health_agent.pilot.food_assessment import calorie_total, render_meal


def test_breakfast_and_lunch_follow_personal_plan():
    breakfast = render_meal({"foods": ["овсянка", "яблоко"], "kcal": 350}, "breakfast")
    assert "✅" in breakfast and "долгие углеводы и фрукт" in breakfast
    assert "≈350 ккал" in breakfast and "⚠️" not in breakfast
    lunch = render_meal(
        {"foods": ["курица", "огурцы", "булгур", "яблоко"], "kcal": 550}, "lunch"
    )
    assert "фрукт" in lunch and "✅" in lunch and "⚠️" not in lunch
    assert "Пропорции" in lunch
    missing = render_meal(
        {"foods": ["курица", "огурцы", "булгур"], "kcal": 450}, "lunch"
    )
    assert "⚠️" in missing and "фрукт" in missing


def test_afternoon_treat_allowed_dinner_vegetables_optional():
    afternoon = render_meal(
        {"foods": ["креветки", "перец", "рис", "зефирка", "печенька"], "kcal": 650},
        "afternoon",
    )
    assert "Вкусняшка учтена" in afternoon
    assert not any(s in afternoon for s in ["наруш", "лишн", "⚠️"])
    no_treat = render_meal({"foods": ["креветки", "перец", "булгур"]}, "afternoon")
    assert "добавь сладкое" not in no_treat and "⚠️" not in no_treat
    dinner = render_meal({"foods": ["рыба"], "kcal": 250}, "dinner")
    assert "✅" in dinner and "овощи — по желанию" in dinner and "⚠️" not in dinner
    extra = render_meal({"foods": ["рыба", "белый хлеб", "торт"]}, "dinner")
    assert "⚠️" in extra and "вне" in extra


def test_refined_breakfast_unknown_foods_and_model_prose_not_trusted():
    refined = render_meal(
        {"foods": ["белый хлеб", "яблочный сок"], "feedback": "Идеально!"}, "breakfast"
    )
    assert "долг" in refined and "фрукт" in refined and "⚠️" in refined
    assert "Идеально" not in refined and "✅" not in refined
    empty = render_meal(None, "lunch")
    assert "сохранён" in empty and "неизвестны" in empty and "✅" not in empty
    assert "✅" not in render_meal(
        {
            "foods": ["неизвестное блюдо"],
            "plate_components": ["protein", "grains", "fruit"],
        },
        "breakfast",
    )


@pytest.mark.parametrize("value", [None, True, -5, float("nan"), float("inf"), "300"])
def test_invalid_calories_never_become_zero_or_a_complete_total(value):
    history = {
        "period": {"to_date": "2026-08-01"},
        "meals": [
            {"category": "breakfast", "nutrients_estimated": {"kcal": 350}},
            {"category": "lunch", "nutrients_estimated": {"kcal": value}},
        ],
    }
    text = calorie_total(history)
    assert "≈350 ккал" in text and "без оценки: 1" in text.lower() and "2 из 4" in text


def setup_coach():
    store, profile = MemoryStore(), uuid4()
    store.put(profile, "food", "settings", "protocol", {"meal_assessment_version": 1})
    brain = Brain(json.dumps({"foods": ["овсянка", "яблоко"], "kcal": 350}))
    return store, profile, brain, FoodCoach(store, brain)


def test_feedback_daily_total_recalculates_without_counting_old_revision():
    store, profile, brain, coach = setup_coach()
    now = datetime(2026, 8, 1, 5, tzinfo=UTC)
    reply = coach.handle(
        profile, "/ел завтрак овсянка и яблоко", source_key="breakfast", now=now
    )
    assert "За 01.08" in reply and "1 из 4" in reply and "≈350 ккал" in reply
    brain.reply = json.dumps({"foods": ["овсянка", "яблоко", "хлеб"], "kcal": 450})
    reply = coach.handle(
        profile, "Добавил хлеб", source_key="bread", now=now + timedelta(minutes=5)
    )
    assert "≈450 ккал" in reply and "800" not in reply and "1 из 4" in reply
    assert "Учтены дополнения: хлеб" in reply
    before = len(brain.calls)
    assert (
        coach.handle(
            profile, "Добавил хлеб", source_key="bread", now=now + timedelta(minutes=5)
        )
        == reply
    )
    assert len(brain.calls) == before
    brain.reply = json.dumps(
        {"foods": ["курица", "огурцы", "булгур", "яблоко"], "kcal": 500}
    )
    lunch = coach.handle(
        profile, "/ел обед курица", source_key="lunch", now=now + timedelta(hours=4)
    )
    assert "≈950 ккал" in lunch and "2 из 4" in lunch
    assert "остаток" not in lunch and "лимит" not in lunch
    day = coach.handle(
        profile, "/сегодня", source_key="summary", now=now + timedelta(hours=5)
    )
    assert "Завтрак" in day and "Обед" in day and "≈950 ккал" in day
    assert "нет записи" in day and "недоел" not in day
    assert len(store.list(profile, "food", "meal")) == 2


def test_midnight_profile_scope_dinner_summary_and_duplicate_categories():
    _store, profile, brain, coach = setup_coach()
    now = datetime(2026, 8, 1, 20, 50, tzinfo=UTC)
    coach.handle(profile, "/ел ужин рыба", source_key="yesterday", now=now)
    other = uuid4()
    coach.handle(other, "/ел завтрак", source_key="other", now=now + timedelta(hours=8))
    brain.reply = json.dumps({"foods": ["рыба"], "kcal": 200})
    reply = coach.handle(
        profile, "/ел ужин рыба", source_key="today", now=now + timedelta(hours=20)
    )
    assert "За 02.08" in reply and "≈200 ккал" in reply and "1 из 4" in reply
    assert "350" not in reply and "Итог" in reply and "нет записи" in reply
    brain.reply = json.dumps({"foods": ["рыба"], "kcal": 100})
    second = coach.handle(
        profile, "/ел ужин рыба", source_key="today2", now=now + timedelta(hours=21)
    )
    assert "≈300 ккал" in second and "1 из 4" in second


def test_zero_and_empty_totals_remain_distinct():
    empty = calorie_total({"period": {"to_date": "2026-08-01"}, "meals": []})
    assert "0 ккал" not in empty and "нет записей" in empty
    zero = calorie_total(
        {
            "period": {"to_date": "2026-08-01"},
            "meals": [
                {"category": "breakfast", "nutrients_estimated": {"kcal": 0}},
            ],
        }
    )
    assert "≈0 ккал" in zero


def test_saved_plan_focus_and_weekly_do_not_ban_planned_treat():
    store, profile, brain, coach = setup_coach()
    setting = store.by_source(profile, "food", "settings", "protocol")
    store.patch(
        profile,
        setting.id,
        {**setting.payload, "long_term_focus": {"desired_fat_loss_kg": 3}},
    )
    now = datetime(2026, 8, 1, 13, tzinfo=UTC)
    plan = coach.handle(profile, "/план", source_key="plan", now=now)
    assert "3 кг за месяц" in plan and "лимита пока нет" in plan and "вкусняшка" in plan
    assert len(brain.calls) == 0
    brain.reply = json.dumps(
        {"foods": ["курица", "огурцы", "булгур", "зефир"], "kcal": 600}
    )
    coach.handle(profile, "/ел полдник", source_key="meal", now=now)
    weekly = coach.handle(profile, "/неделя", source_key="week", now=now)
    assert "Вкусняшка в полдник разрешена" in weekly
    assert "/фокус" in weekly
    assert "замени сладость" not in weekly


def test_text_focus_supersedes_legacy_numeric_aspiration():
    from health_agent.pilot.food_assessment import plan_text

    text = plan_text({"long_term_focus": {
        "title": "Август: регулярное питание и прогулки",
        "desired_fat_loss_kg": 3,
    }})
    assert "Август: регулярное питание и прогулки" in text
    assert "3 кг" not in text and "лимита пока нет" in text


def test_partial_same_category_and_truncation_are_visible():
    from health_agent.pilot.food_assessment import daily_summary

    history = {
        "period": {"to_date": "2026-08-01"},
        "truncated": True,
        "meals": [
            {"category": "breakfast", "nutrients_estimated": {"kcal": 350}},
            {"category": "breakfast", "nutrients_estimated": {"kcal": None}},
        ],
    }
    reply = daily_summary(history)
    assert "1 из 4" in reply and "часть записей" in reply
    assert "Завтрак: ≈350 ккал + есть еда без оценки" in reply


def test_breakfast_protein_optional_and_dinner_egg_preference():
    with_protein = render_meal({"foods": ["овсянка", "яблоко", "яйца"]}, "breakfast")
    assert "белок тоже есть" in with_protein and "⚠️" not in with_protein
    without = render_meal({"foods": ["овсянка", "яблоко"]}, "breakfast")
    assert "⚠️" not in without and "нужен белок" not in without
    for food in ["яйца без желтков", "яичные белки", "белковый омлет"]:
        text = render_meal({"foods": [food], "kcal": 100}, "dinner")
        assert "без желтков — как ты предпочитаешь" in text
        assert "⚠️" not in text and "не вижу" not in text
    whole = render_meal({"foods": ["целые яйца", "желтки"], "kcal": 200}, "dinner")
    assert "предпочитаешь без них" in whole and "запрещ" not in whole
    unknown = render_meal({"foods": ["омлет"], "kcal": 200}, "dinner")
    assert "неясно" in unknown and "желтки записаны" not in unknown


def test_weekly_results_coverage_partial_calories_and_one_focus():
    from health_agent.pilot.food_assessment import weekly_summary

    def meal(at, category, foods, kcal):
        return {
            "recorded_at": at,
            "category": category,
            "foods": foods,
            "analysis_available": True,
            "nutrients_estimated": {"kcal": kcal},
        }

    history = {
        "period": {"from_date": "2026-08-01", "to_date": "2026-08-07", "days": 7},
        "meals": [
            meal("2026-08-01T06:00:00+00:00", "breakfast", ["овсянка", "яблоко"], 350),
            meal(
                "2026-08-01T09:00:00+00:00",
                "lunch",
                ["курица", "булгур", "огурец"],
                500,
            ),
            meal(
                "2026-08-01T13:00:00+00:00",
                "afternoon",
                ["курица", "булгур", "огурец", "зефир"],
                600,
            ),
            meal("2026-08-01T17:00:00+00:00", "dinner", ["рыба"], 200),
            meal(
                "2026-08-02T09:00:00+00:00",
                "lunch",
                ["курица", "булгур", "огурец"],
                None,
            ),
        ],
    }
    report = weekly_summary(history)
    assert "5 приёмов за 2 из 7 дней" in report and "в 1 дн." in report
    assert "≈1 650 ккал" in report and "Без оценки: 1" in report
    assert "завтрак 1/1" in report and "полдник 1/1" in report and "обед 0/2" in report
    assert "фрукт" in report and "(2 раз)" in report
    assert report.count("🎯") == 1 and "размер порции" in report
    assert "не калории всего рациона" in report and "Потерю жира" in report


def test_weekly_automatic_notice_uses_same_summary_and_delivery_dedup():
    store, profile, brain, coach = setup_coach()
    now = datetime(2026, 8, 2, 15, tzinfo=UTC)  # Sunday 18:00 Moscow
    brain.reply = json.dumps({"foods": ["рыба"], "kcal": 200})
    coach.handle(profile, "/ел ужин рыба", source_key="dinner", now=now)
    notice = next(n for n in coach.due(profile, now) if n.key.startswith("food-weekly-"))
    assert notice.text == coach.handle(profile, "/неделя", source_key="week", now=now)
    assert "📊 Неделя" in notice.text and "✅" in notice.text
    store.put(profile, "food", "notice", notice.key, {"delivered": True}, at=now)
    assert not [n for n in coach.due(profile, now) if n.key.startswith("food-weekly-")]
