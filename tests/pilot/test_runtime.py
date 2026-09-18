import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from health_agent.config import Settings
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.brain import PilotBrain
from health_agent.pilot.contracts import Notice
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.goals import goal_action
from health_agent.pilot.runtime import (
    HELP,
    PilotActions,
    PilotInbox,
    PilotQuestions,
    dispatch_notices,
    maybe_coros_sync,
    run_pilot_cycle_tasks,
)
from health_agent.pilot.storage import PilotStore
from health_agent.questions.replies import PrivateReplyStore
from health_agent.questions.safety import URGENT_RESPONSE
from health_agent.telegram.actions import PreparedTelegramTextActions
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.service import TelegramUpdateService
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import RemoteFile, TelegramIdentity

NOW = datetime(2026, 9, 7, 9, tzinfo=UTC)


class Gateway:
    def __init__(self):
        self.sent = []
        self.data = b"\xff\xd8\xfftest-photo"

    def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))
        return len(self.sent)

    def get_file(self, file_id):
        return RemoteFile(file_id, "photo-unique", "photo.jpg", len(self.data))

    def download_chunks(self, path):
        yield self.data


class SDKClient:
    """Deterministic SDK boundary double; the real brain owns model metadata."""

    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        content = json.dumps(
            {
                "foods": ["рис", "овощи"],
                "portion_estimate": "одна тарелка",
                "kcal": None,
                "protein_g": None,
                "fat_g": None,
                "carbs_g": None,
                "saturated_fat_g": None,
                "fiber_g": None,
                "cholesterol_mg": None,
                "confidence": 0.5,
                "unknowns": ["масса продуктов"],
                "feedback": "В записи есть рис и овощи.",
            },
            ensure_ascii=False,
        )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        role="assistant",
                        refusal=None,
                        tool_calls=None,
                        function_call=None,
                        content=content,
                    ),
                )
            ]
        )


class Coach:
    def __init__(self, store):
        self.store = store
        self.calls = []

    def handle(self, profile, text, *, source_key, now, attachment=None):
        self.calls.append((profile, text, attachment))
        self.store.put(profile, "food", "meal", source_key, {"text": text}, at=now)
        return "Приём сохранён"

    def due(self, profile, now):
        return (
            []
            if self.store.list(profile, "food", "notice")
            else [Notice("meal1", "Пора поесть")]
        )


def setup_runtime(tmp_path, clean_database):
    store = PilotStore(clean_database)
    coach = Coach(store)
    actions = PilotActions(coach, store, "food", DEFAULT_PROFILE_ID)
    replies = PrivateReplyStore(tmp_path / "replies")
    state = SqliteTelegramState(tmp_path / "state.sqlite3", clock=lambda: NOW)
    state.register_bot(111, "food_test")
    state.bind_identity(111, TelegramIdentity(101, DEFAULT_PROFILE_ID, 101))
    state.bind_identity(111, TelegramIdentity(202, uuid4(), 202))
    gateway = Gateway()
    messenger = TelegramMessenger(111, gateway, state)
    service = TelegramUpdateService(
        111,
        gateway,
        state,
        messenger,
        PilotQuestions(actions, replies),
        SimpleNamespace(status=lambda _: "status", sync=lambda _: "sync"),
        PilotInbox(
            coach, store, "food", tmp_path, SimpleNamespace(),
            configured_profile_id=DEFAULT_PROFILE_ID,
        ),
        staging_root=tmp_path / "staging",
        clock=lambda: NOW,
        text_actions=PreparedTelegramTextActions(actions, replies),
        help_text="Помощник по еде",
    )
    return service, store, coach, gateway, messenger


def update(number, text=None, *, photo=False, user=101):
    message = {
        "message_id": number,
        "date": int(NOW.timestamp()),
        "from": {"id": user, "is_bot": False},
        "chat": {"id": user, "type": "private"},
    }
    if photo:
        message.update(
            photo=[
                {
                    "file_id": "photo",
                    "file_unique_id": "photo-unique",
                    "width": 30,
                    "height": 30,
                }
            ],
            caption=text,
        )
    else:
        message["text"] = text
    return {"update_id": number, "message": message}


def test_telegram_postgres_replay_caption_and_identity(tmp_path, clean_database):
    service, _store, coach, gateway, _ = setup_runtime(tmp_path, clean_database)
    service.process_update(update(1, "/start"))
    assert gateway.sent[-1][1] == "Помощник по еде"
    photo = update(2, "Обед в 12:00", photo=True)
    assert service.process_update(photo).terminal
    service.process_update(photo)
    assert len(coach.calls) == 1
    assert coach.calls[0][1] == "Обед в 12:00"
    assert coach.calls[0][2].path.read_bytes() == gateway.data
    assert len(PilotStore(clean_database).list(DEFAULT_PROFILE_ID, "food", "meal")) == 1
    service.process_update(update(3, "Чужие данные", user=202))
    assert len(coach.calls) == 1
    assert "другого профиля" in gateway.sent[-1][1]
    service.process_update(update(4, "Чужое фото", photo=True, user=202))
    assert len(coach.calls) == 1
    assert "другого профиля" in gateway.sent[-1][1]


def test_real_food_pipeline_delivers_reply_and_persists_model_metadata(
    tmp_path, clean_database
):
    store = PilotStore(clean_database)
    client = SDKClient()
    settings = Settings(
        yandex_folder_id="test",
        yandex_allowed_profile_ids=(DEFAULT_PROFILE_ID,),
    )
    brain = PilotBrain(
        settings,
        DEFAULT_PROFILE_ID,
        client=client,
        store=store,
        domain="food",
    )
    coach = FoodCoach(store, brain)
    actions = PilotActions(coach, store, "food")
    replies = PrivateReplyStore(tmp_path / "real-replies")
    state = SqliteTelegramState(tmp_path / "real-state.sqlite3", clock=lambda: NOW)
    state.register_bot(111, "food_test")
    state.bind_identity(111, TelegramIdentity(101, DEFAULT_PROFILE_ID, 101))
    other_profile = uuid4()
    state.bind_identity(111, TelegramIdentity(202, other_profile, 202))
    gateway = Gateway()
    messenger = TelegramMessenger(111, gateway, state)
    service = TelegramUpdateService(
        111,
        gateway,
        state,
        messenger,
        PilotQuestions(PilotActions(coach, store, "food", DEFAULT_PROFILE_ID), replies),
        SimpleNamespace(status=lambda _: "status", sync=lambda _: "sync"),
        PilotInbox(
            coach, store, "food", tmp_path, brain,
            configured_profile_id=DEFAULT_PROFILE_ID,
        ),
        staging_root=tmp_path / "real-staging",
        clock=lambda: NOW,
        text_actions=PreparedTelegramTextActions(actions, replies),
        help_text="Помощник по еде",
    )

    assert service.process_update(update(20, "Обед: рис и овощи", photo=True)).terminal

    assert gateway.sent[-1][1].startswith("В записи отмечены: овощи, крупы или хлеб.")
    assert "15:50" in gateway.sent[-1][1]
    assert len(client.calls) == 1
    attachment = store.list(DEFAULT_PROFILE_ID, "food", "attachment")[0]
    meal = store.list(DEFAULT_PROFILE_ID, "food", "meal")[0]
    model_run = store.list(DEFAULT_PROFILE_ID, "food", "model_run")[0]
    assert attachment.payload["caption"] == "Обед: рис и овощи"
    assert attachment.payload["reply"] == gateway.sent[-1][1]
    assert meal.payload["analysis"]["foods"] == ["рис", "овощи"]
    assert model_run.payload["status"] == "completed"
    assert model_run.payload["model"] == client.calls[0]["model"]
    assert model_run.payload["output"]

    assert service.process_update(update(21, "секрет второго профиля", user=202)).terminal
    assert len(client.calls) == 1
    assert not store.list(other_profile, "food")


def test_sleep_help_preserves_existing_commands_and_adds_sleep_commands():
    assert "/review" in HELP["sleep"]
    assert "/visits" in HELP["sleep"]
    assert "/sync" in HELP["sleep"]
    assert "/дневник" in HELP["sleep"]
    assert "/утро" in HELP["sleep"]


def test_daily_coros_sync_is_bounded_and_skips_completed_day(
    monkeypatch, tmp_path, clean_database
):
    from health_agent.pilot import coros_auth, coros_sync

    store = PilotStore(clean_database)
    calls = []

    class Auth:
        def __init__(self, root):
            self.root = root

        def status(self):
            return True

    def sync(store_arg, auth, profile, root, since, until):
        calls.append((auth.root, root, since, until))
        store_arg.put(
            profile,
            "training",
            "sync_run",
            "daily-test",
            {"status": "success", "until": until.isoformat()},
            at=NOW,
        )
        return {"stored": 0}

    monkeypatch.setattr(coros_auth, "CorosOAuth", Auth)
    monkeypatch.setattr(coros_sync, "run_coros_sync", sync)
    auth_root = tmp_path / "coros"

    assert maybe_coros_sync(store, DEFAULT_PROFILE_ID, auth_root, NOW)
    assert not maybe_coros_sync(store, DEFAULT_PROFILE_ID, auth_root, NOW)
    assert calls == [(auth_root, auth_root, NOW.date() - timedelta(days=6), NOW.date())]


def test_failed_coros_sync_cools_down_across_restart_without_starving_notice(
    monkeypatch, tmp_path, clean_database
):
    from health_agent.pilot import coros_auth, coros_sync

    store = PilotStore(clean_database)
    sync_calls = []

    class Auth:
        def __init__(self, root):
            self.root = root

        def status(self):
            return True

    def fail_sync(*args, **kwargs):
        sync_calls.append((args, kwargs))
        raise RuntimeError("private provider detail")

    class TrainingCoach:
        def handle(self, profile, text, *, source_key, now, attachment=None):
            return "unused"

        def due(self, profile, now):
            if store.list(profile, "training", "notice"):
                return []
            return [Notice("weekly", "Пора подвести итоги")]

    monkeypatch.setattr(coros_auth, "CorosOAuth", Auth)
    monkeypatch.setattr(coros_sync, "run_coros_sync", fail_sync)
    state = SimpleNamespace(
        errors=[], record_poll=lambda bot, code: state.errors.append(code)
    )
    telegram_state = SqliteTelegramState(
        tmp_path / "failure-state.sqlite3", clock=lambda: NOW
    )
    telegram_state.register_bot(111, "training_test")
    telegram_state.bind_identity(111, TelegramIdentity(101, DEFAULT_PROFILE_ID, 101))
    gateway = Gateway()
    messenger = TelegramMessenger(111, gateway, telegram_state)
    auth_root = tmp_path / "coros-failure"

    assert (
        run_pilot_cycle_tasks(
            TrainingCoach(),
            store,
            messenger,
            state,
            111,
            DEFAULT_PROFILE_ID,
            "training",
            NOW,
            coros_root=auth_root,
        )
        == 1
    )
    assert gateway.sent == [(101, "Пора подвести итоги")]
    assert state.errors == ["pilot_coros_RuntimeError"]
    failure = store.list(DEFAULT_PROFILE_ID, "training", "sync_run")[0]
    assert failure.payload["status"] == "failed"
    assert failure.payload["failure"] == "RuntimeError"
    assert "private provider detail" not in str(failure.payload)

    restarted_store = PilotStore(clean_database)
    assert (
        run_pilot_cycle_tasks(
            TrainingCoach(),
            restarted_store,
            messenger,
            state,
            111,
            DEFAULT_PROFILE_ID,
            "training",
            NOW + timedelta(minutes=30),
            coros_root=auth_root,
        )
        == 0
    )
    assert len(sync_calls) == 1
    assert gateway.sent == [(101, "Пора подвести итоги")]


def test_urgent_input_preserved_without_coach(tmp_path, clean_database):
    service, store, coach, gateway, _ = setup_runtime(tmp_path, clean_database)
    service.process_update(update(1, "Не могу дышать"))
    assert not coach.calls
    assert gateway.sent[-1][1] == URGENT_RESPONSE
    assert (
        store.list(DEFAULT_PROFILE_ID, "food", "input")[0].payload["time_source"]
        == "telegram"
    )


def test_notice_mark_only_after_delivery_and_no_repeat(tmp_path, clean_database):
    _, store, coach, gateway, messenger = setup_runtime(tmp_path, clean_database)
    failing = SimpleNamespace(
        send_to_profile=lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("offline")
        )
    )
    with pytest.raises(RuntimeError):
        dispatch_notices(coach, store, failing, DEFAULT_PROFILE_ID, "food", NOW)
    assert not store.list(DEFAULT_PROFILE_ID, "food", "notice")
    assert (
        dispatch_notices(coach, store, messenger, DEFAULT_PROFILE_ID, "food", NOW) == 1
    )
    assert (
        dispatch_notices(
            Coach(PilotStore(clean_database)),
            store,
            messenger,
            DEFAULT_PROFILE_ID,
            "food",
            NOW,
        )
        == 0
    )
    assert len(gateway.sent) == 1


def test_goal_hierarchy_changes_only_target_and_isolates(clean_database):
    store = PilotStore(clean_database)

    def action(text, key):
        return goal_action(
            store, DEFAULT_PROFILE_ID, "training", text, source_key=key, now=NOW
        )

    action("/цель год | Ориентир года", "year")
    parent = store.list(DEFAULT_PROFILE_ID, "shared", "goal")[0]
    action(f"/цель неделя | План недели | {parent.id[:8]}", "week")
    child = next(
        r
        for r in store.list(DEFAULT_PROFILE_ID, "shared", "goal")
        if r.source_key == "week"
    )
    action(f"/изменить {child.id[:8]} | Новая неделя", "edit")
    assert store.get(DEFAULT_PROFILE_ID, parent.id).payload["title"] == "Ориентир года"
    assert store.get(DEFAULT_PROFILE_ID, child.id).payload["parent_id"] == parent.id
    assert "Не получилось" in action(
        f"/цель год | Неверный родитель | {child.id[:8]}", "invalid"
    )
    assert "Не получилось" in goal_action(
        store,
        uuid4(),
        "training",
        f"/пауза {parent.id[:8]}",
        source_key="foreign",
        now=NOW + timedelta(seconds=1),
    )


def test_main_bot_question_route_saves_sleep_without_morning_prompt(tmp_path, clean_database):
    from health_agent.pilot.sleep import SleepCoach
    from health_agent.telegram.types import HealthQuestion, MessageContext

    store = PilotStore(clean_database)

    def offline_brain(system, payload, *, image_path=None):
        raise RuntimeError('offline')

    def questions():
        return PilotQuestions(
            PilotActions(SleepCoach(store, offline_brain), store, 'sleep', DEFAULT_PROFILE_ID),
            PrivateReplyStore(tmp_path / 'sleep-replies'),
        )

    context = MessageContext(111, DEFAULT_PROFILE_ID, 101, 101, 1, 10, NOW, NOW + timedelta(minutes=3))
    question = HealthQuestion(context, 'Сегодня тяжеловато просыпался. Ночью вставал один раз.')
    reply = questions().answer(question)
    assert reply.startswith('Запись сна сохранена.')
    assert questions().answer(question) == reply  # restart and update replay
    entries = store.list(DEFAULT_PROFILE_ID, 'sleep', 'diary')
    assert len(entries) == 1
    assert entries[0].payload['text'] == question.text
    assert entries[0].at == NOW  # original Telegram time, not delayed processing time
    assert not store.list(DEFAULT_PROFILE_ID, 'sleep', 'notice')
    # A Telegram update is immutable: use a fresh update for the diary command.
    from dataclasses import replace
    diary = questions().answer(HealthQuestion(replace(context, update_id=11, message_id=2), '/дневник'))
    assert question.text in diary


def test_shared_cycle_routes_before_food_and_preserves_profile_scope(tmp_path, clean_database):
    from health_agent.pilot.weekly_cycle import WeeklyCycle
    service, store, coach, gateway, _ = setup_runtime(tmp_path, clean_database)
    store.put(DEFAULT_PROFILE_ID, 'shared', 'settings', 'coaching-cycle',
              {'enabled': True, 'training_sessions': 2}, at=NOW)
    service.process_update(update(100, '/цикл'))
    assert 'Черновик общей недели 2026-09-14' in gateway.sent[-1][1]
    service.process_update(update(101, 'Принять неделю 14.09.2026'))
    service.process_update(update(101, 'Принять неделю 14.09.2026'))
    service.process_update(update(102, '/вес 75,2'))
    service.process_update(update(103, '/тренировка 2026-09-06 ходьба'))
    assert not coach.calls and not store.list(DEFAULT_PROFILE_ID, 'food', 'meal')
    assert len(store.list(DEFAULT_PROFILE_ID, 'shared', 'cycle_plan')) == 1
    assert len(store.list(DEFAULT_PROFILE_ID, 'shared', 'weight')) == 1
    assert len(store.list(DEFAULT_PROFILE_ID, 'training', 'manual_activity')) == 1
    assert not WeeklyCycle(store).due(uuid4(), NOW)
    service.process_update(update(104, '/цикл', user=202))
    assert 'другого профиля' in gateway.sent[-1][1]


def test_shared_welcome_delivery_recovers_and_remains_once_on_restart(tmp_path, clean_database):
    _, store, _, gateway, messenger = setup_runtime(tmp_path, clean_database)
    coach = SimpleNamespace(due=lambda profile, now: [])
    store.put(DEFAULT_PROFILE_ID, 'shared', 'settings', 'coaching-cycle',
              {'enabled': True, 'training_sessions': 2}, at=NOW)
    failing = SimpleNamespace(send_to_profile=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('offline')))
    with pytest.raises(RuntimeError):
        dispatch_notices(coach, store, failing, DEFAULT_PROFILE_ID, 'sleep', NOW)
    assert not store.by_source(DEFAULT_PROFILE_ID, 'sleep', 'notice', 'cycle:welcome')
    assert dispatch_notices(coach, store, messenger, DEFAULT_PROFILE_ID, 'food', NOW) == 0
    assert dispatch_notices(coach, store, messenger, DEFAULT_PROFILE_ID, 'training', NOW) == 0
    assert dispatch_notices(coach, store, messenger, DEFAULT_PROFILE_ID, 'sleep', NOW) == 1
    assert dispatch_notices(coach, PilotStore(clean_database), messenger, DEFAULT_PROFILE_ID, 'sleep', NOW) == 0
    assert len(gateway.sent) == 1 and 'Черновик общей недели' in gateway.sent[0][1]
    assert not store.list(DEFAULT_PROFILE_ID, 'shared', 'cycle_plan')


def test_cycle_suppresses_domain_weeklies_but_keeps_ordinary_reminders(clean_database):
    from health_agent.pilot.sleep import SleepCoach
    from health_agent.pilot.training import TrainingCoach
    store = PilotStore(clean_database)
    store.put(DEFAULT_PROFILE_ID, 'shared', 'settings', 'coaching-cycle', {'enabled': True}, at=NOW)
    brain = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError('No model call expected'))
    sunday = datetime(2026, 9, 13, 15, tzinfo=UTC)
    food, sleep, training = FoodCoach(store, brain), SleepCoach(store, brain), TrainingCoach(store, brain)
    assert food._weekly_notice(DEFAULT_PROFILE_ID, sunday) is None
    assert training.due(DEFAULT_PROFILE_ID, sunday) == []
    assert not any('weekly' in n.key for n in sleep.due(DEFAULT_PROFILE_ID, sunday))
    breakfast = datetime(2026, 9, 14, 6, tzinfo=UTC)
    assert any(n.key.startswith('breakfast:') for n in food.due(DEFAULT_PROFILE_ID, breakfast))
    assert sleep.due(DEFAULT_PROFILE_ID, breakfast.replace(hour=5))


def test_main_api_attaches_cycle_buttons_and_preserves_sleep_keyboard(monkeypatch):
    from health_agent.pilot.runtime import SleepTelegramAPI
    from health_agent.pilot.sleep_checkin import PROMPT
    from health_agent.telegram.api import TelegramBotAPI
    calls = []
    monkeypatch.setattr(TelegramBotAPI, 'send_message', lambda self, chat_id, text, **kwargs: calls.append(kwargs) or 1)
    api = SleepTelegramAPI('123:test')
    api.send_message(123, 'Черновик общей недели 2026-09-14 · 14.09–20.09.')
    api.send_message(123, PROMPT)
    api.send_message(123, 'Общая неделя принята.\n...')
    assert calls[0]['reply_markup']['keyboard'][0] == ['Принять неделю 14.09.2026']
    assert calls[1]['reply_markup']['keyboard'][0][0] == 'Сон: 0'
    assert calls[2]['reply_markup'] == {'remove_keyboard': True}
