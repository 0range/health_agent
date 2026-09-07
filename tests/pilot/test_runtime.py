from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.contracts import Notice
from health_agent.pilot.goals import goal_action
from health_agent.pilot.runtime import (
    PilotActions,
    PilotInbox,
    PilotQuestions,
    dispatch_notices,
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
    actions = PilotActions(coach, store, "food")
    replies = PrivateReplyStore(tmp_path / "replies")
    state = SqliteTelegramState(tmp_path / "state.sqlite3", clock=lambda: NOW)
    state.register_bot(111, "food_test")
    state.bind_identity(111, TelegramIdentity(101, DEFAULT_PROFILE_ID, 101))
    gateway = Gateway()
    messenger = TelegramMessenger(111, gateway, state)
    service = TelegramUpdateService(
        111,
        gateway,
        state,
        messenger,
        PilotQuestions(actions, replies),
        SimpleNamespace(status=lambda _: "status", sync=lambda _: "sync"),
        PilotInbox(coach, store, "food", tmp_path, SimpleNamespace()),
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
