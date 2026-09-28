from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from test_training import MemoryStore

from health_agent.pilot.body_checkin import due, handle, keyboard

NOW = datetime(2026, 9, 28, 10, tzinfo=UTC)


class Conversation:
    def __init__(self):
        self.store, self.profile, self.count = MemoryStore(), uuid4(), 0

    def say(self, text, *, now=NOW, key=None, profile=None):
        self.count += 1
        return handle(self.store, profile or self.profile, text, key or str(self.count), now)

    def records(self, kind):
        return self.store.list(self.profile, "shared", kind)

    def ready(self, text="/замер 2026-09-27 вес 76,2 кг, жир 20,1%, мышцы 57 кг"):
        reply = self.say(text)
        assert "Проверь" in reply
        return keyboard(reply)["keyboard"][0][0]


def test_three_questions_date_preview_and_persistence_only_after_confirmation():
    c = Conversation()
    assert "1/3" in c.say("/замер 2026-09-27")
    assert "2/3" in c.say("76,2 кг")
    assert "3/3" in c.say("20,1%")
    reply = c.say("57 кг")
    assert "27.09.2026" in reply and "76.2 кг" in reply
    assert not c.records("weight") and not c.records("body_measurement")
    result = c.say(keyboard(reply)["keyboard"][0][0])
    assert result.startswith("Замер сохранён")
    row = c.records("body_measurement")[0]
    assert row.payload["weight_kg"] == 76.2
    assert row.payload["body_fat_percent"] == 20.1
    assert row.payload["muscle_mass_kg"] == 57
    assert row.payload["muscle_percent"] is None
    assert row.payload["measurement_date"] == "2026-09-27"
    assert row.payload["timestamp_precision"] == "day"
    assert row.at == datetime(2026, 9, 26, 21, tzinfo=UTC)
    assert row.payload["recorded_at"] == NOW.isoformat()
    assert c.records("weight")[0].payload["weight_kg"] == 76.2


def test_one_message_decimal_commas_muscle_percentage_stays_percentage():
    c = Conversation()
    button = c.ready("76 кг, жир 20%, мышцы 42,5%")
    c.say(button)
    p = c.records("body_measurement")[0].payload
    assert p["muscle_percent"] == 42.5 and p["muscle_mass_kg"] is None


def test_values_before_labels_and_spoken_muscle_label():
    c = Conversation()
    button = c.ready("75.1 кг вес, 21,3% жир, 55.4 масса мышц")
    c.say(button)
    p = c.records("body_measurement")[0].payload
    assert (p["weight_kg"], p["body_fat_percent"], p["muscle_mass_kg"]) == (75.1, 21.3, 55.4)


def test_skips_do_not_become_zero_and_weight_is_optional():
    c = Conversation()
    c.say("/замер")
    c.say("пропустить")
    c.say("21%")
    reply = c.say("пропустить")
    c.say(keyboard(reply)["keyboard"][0][0])
    assert c.records("body_measurement")[0].payload["weight_kg"] is None
    assert not c.records("weight")


def test_all_skips_do_not_create_an_empty_measurement():
    c = Conversation()
    c.say("/замер")
    c.say("пропустить")
    c.say("пропустить")
    assert "пустой" in c.say("пропустить")
    assert not c.records("body_measurement")


@pytest.mark.parametrize("text", ["/замер 2026-09-29", "/замер 2026-02-30", "/замер 27.09.2026 лишнее", "/замер вес -76 кг, жир 20%, мышцы 57 кг", "/замер вес 76 кг, жир 120%, мышцы 57 кг"])
def test_invalid_dates_and_numbers_cannot_be_saved(text):
    c = Conversation()
    reply = c.say(text)
    assert reply and "Проверь" not in reply
    assert not c.records("body_measurement")
    assert not c.records("body_checkin")


def test_partial_input_is_atomic_and_units_must_match_the_question():
    c = Conversation()
    c.say("/замер")
    assert "кг" in c.say("76%")
    assert "1/3" in c.say("/замер")
    c.say("76")
    assert "процент" in c.say("20 кг")
    assert "2/3" in c.say("/замер")


def test_unrelated_commands_sleep_buttons_and_questions_are_not_consumed():
    c = Conversation()
    assert c.say("вес 76") is None  # Existing direct weight command remains intact.
    c.say("/замер")
    for text in ("Сон: 8", "Выспался: 7", "/опрос", "/инсайты", "Как я спал?", "/вес 76"):
        assert c.say(text) is None


def test_telegram_replay_and_double_save_create_one_observation():
    c = Conversation()
    reply = c.say("/замер", key="start")
    c.say("76", key="weight")
    assert c.say("/замер", key="start") == reply
    c.say("20")
    result = c.say("57")
    button = keyboard(result)["keyboard"][0][0]
    saved = c.say(button, key="save")
    assert c.say(button, key="save") == saved
    assert c.say(button) == saved
    assert len(c.records("body_measurement")) == len(c.records("weight")) == 1


def test_confirmation_cannot_cross_profiles_or_save_a_newer_draft():
    c = Conversation()
    button = c.ready()
    assert "Нет" in c.say(button, profile=uuid4())
    c.say("/замер отмена")
    new_button = c.ready()
    assert button != new_button
    assert "устарела" in c.say(button)
    assert not c.records("body_measurement")
    c.say(new_button)
    assert len(c.records("body_measurement")) == 1


def test_expired_draft_does_not_capture_new_numbers():
    c = Conversation()
    button = c.ready()
    assert "истёк" in c.say(button, now=NOW+timedelta(days=2))
    assert c.say("80", now=NOW+timedelta(days=2)) is None
    assert not c.records("body_measurement")


def test_edit_keeps_date_and_requires_fresh_numbers_before_save():
    c = Conversation()
    button = c.ready()
    edit = button.replace("сохранить", "исправить")
    assert "1/3" in c.say(edit)
    c.say("75")
    c.say("19")
    reply = c.say("56")
    assert "27.09.2026" in reply
    c.say(keyboard(reply)["keyboard"][0][0])
    assert c.records("weight")[0].payload["weight_kg"] == 75


def test_retry_after_projection_failure_completes_without_duplicate_observation():
    c = Conversation()
    button = c.ready()
    put = c.store.put

    def fail_weight(*args, **kwargs):
        if args[2] == "weight":
            raise RuntimeError("simulated storage failure")
        return put(*args, **kwargs)

    c.store.put = fail_weight
    with pytest.raises(RuntimeError):
        c.say(button, key="save")
    assert len(c.records("body_measurement")) == 1
    c.store.put = put
    c.say(button, key="save")
    assert len(c.records("body_measurement")) == len(c.records("weight")) == 1


def test_retry_after_answer_patch_does_not_reinterpret_weight_as_fat():
    c = Conversation()
    c.say("/замер")
    put = c.store.put

    def fail_reply(*args, **kwargs):
        if args[2] == "body_reply":
            raise RuntimeError("simulated reply persistence failure")
        return put(*args, **kwargs)

    c.store.put = fail_reply
    with pytest.raises(RuntimeError):
        c.say("76", key="weight")
    c.store.put = put
    assert "2/3" in c.say("76", key="weight")
    assert c.records("body_checkin")[0].payload["body_fat_percent"] is None


def test_reminders_opt_in_moscow_weekdays_time_window_profile_and_receipt():
    c = Conversation()
    thursday = datetime(2026, 10, 1, 5, tzinfo=UTC)
    assert not due(c.store, c.profile, thursday)
    assert "08:00" in c.say("/замер напоминания вкл")
    for at in (thursday-timedelta(seconds=1), thursday+timedelta(hours=4), thursday+timedelta(days=1)):
        assert not due(c.store, c.profile, at)
    assert not due(c.store, uuid4(), thursday)
    notice = due(c.store, c.profile, thursday)[0]
    assert notice.key == "body:reminder:2026-10-01"
    assert keyboard(notice.text)["keyboard"] == [["Начать замер"]]
    c.store.put(c.profile, "sleep", "notice", notice.key, {"delivered": True}, at=thursday)
    assert not due(c.store, c.profile, thursday+timedelta(minutes=1))
    assert len(due(c.store, c.profile, thursday+timedelta(days=3))) == 1
    c.say("/замер напоминания выкл")
    assert not due(c.store, c.profile, thursday+timedelta(days=3))


def test_completed_today_measurement_suppresses_reminder():
    c = Conversation()
    sunday = datetime(2026, 9, 27, 5, tzinfo=UTC)
    c.say("/замер напоминания вкл", now=sunday)
    result = c.say("/замер вес 76 кг, жир 20%, мышцы 57 кг", now=sunday)
    c.say(keyboard(result)["keyboard"][0][0], now=sunday)
    assert not due(c.store, c.profile, sunday)


def test_main_actions_route_checkin_and_preserve_foreign_profile_guard():
    from types import SimpleNamespace

    from health_agent.pilot.runtime import PilotActions
    from health_agent.telegram.types import MessageContext

    c = Conversation()
    actions = PilotActions(SimpleNamespace(), c.store, "sleep", c.profile)
    context = MessageContext(111, c.profile, 1, 1, 1, 1, NOW, NOW)
    assert "1/3" in actions.handle(context, "/замер")
    foreign = MessageContext(111, uuid4(), 2, 2, 2, 2, NOW, NOW)
    assert "другого профиля" in actions.handle(foreign, "/замер")
    assert not c.store.list(foreign.profile_id, "shared", "body_checkin")


def test_main_api_adds_measurement_buttons(monkeypatch):
    from health_agent.pilot.runtime import SleepTelegramAPI
    from health_agent.telegram.api import TelegramBotAPI

    c = Conversation()
    prompt = c.say("/замер")
    calls = []
    monkeypatch.setattr(TelegramBotAPI, "send_message", lambda self, chat_id, text, **kwargs: calls.append(kwargs) or 1)
    api = object.__new__(SleepTelegramAPI)
    api.send_message(123, prompt)
    assert "пропустить вес" in calls[0]["reply_markup"]["keyboard"][0][0]
