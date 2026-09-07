"""User-level photo/comment journey through Telegram and disposable PostgreSQL."""

from datetime import timedelta
from types import SimpleNamespace

from test_runtime import NOW, Gateway, SDKClient, update

from health_agent.config import Settings
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.brain import PilotBrain
from health_agent.pilot.food import FoodCoach
from health_agent.pilot.runtime import (
    PilotActions,
    PilotInbox,
    PilotQuestions,
    dispatch_notices,
)
from health_agent.pilot.storage import PilotStore
from health_agent.questions.replies import PrivateReplyStore
from health_agent.telegram.actions import PreparedTelegramTextActions
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.service import TelegramUpdateService
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import TelegramIdentity


def test_two_photos_comments_restart_and_weekly_delivery(tmp_path, clean_database):
    clock = [NOW]
    store = PilotStore(clean_database)
    client = SDKClient()
    brain = PilotBrain(
        Settings(
            _env_file=None,
            yandex_folder_id="synthetic",
            yandex_allowed_profile_ids=(DEFAULT_PROFILE_ID,),
        ),
        DEFAULT_PROFILE_ID,
        client=client,
        store=store,
        domain="food",
    )
    coach = FoodCoach(store, brain)
    state = SqliteTelegramState(tmp_path / "telegram.sqlite3", clock=lambda: clock[0])
    state.register_bot(111, "synthetic_food")
    state.bind_identity(111, TelegramIdentity(101, DEFAULT_PROFILE_ID, 101))
    gateway = Gateway()
    messenger = TelegramMessenger(111, gateway, state)
    replies = PrivateReplyStore(tmp_path / "replies")
    actions = PilotActions(coach, store, "food", DEFAULT_PROFILE_ID)
    service = TelegramUpdateService(
        111,
        gateway,
        state,
        messenger,
        PilotQuestions(actions, replies),
        SimpleNamespace(status=lambda _: "status", sync=lambda _: "sync"),
        PilotInbox(
            coach, store, "food", tmp_path, brain,
            configured_profile_id=DEFAULT_PROFILE_ID,
        ),
        staging_root=tmp_path / "staging",
        text_actions=PreparedTelegramTextActions(actions, replies),
        clock=lambda: clock[0],
    )

    def incoming(number, text=None, *, photo=False, minutes=0):
        clock[0] = NOW + timedelta(minutes=minutes)
        payload = update(number, text, photo=photo)
        payload["message"]["date"] = int(clock[0].timestamp())
        assert service.process_update(payload).terminal
        return payload

    incoming(401, "Обед", photo=True)
    first = store.list(DEFAULT_PROFILE_ID, "food", "meal")[0]
    gateway.data = b"\xff\xd8\xffsecond-synthetic-view"
    incoming(402, "Десерт", photo=True, minutes=20)
    incoming(403, "Там ещё было немного масла", minutes=22)
    comment = incoming(404, "Порция примерно 250 г", minutes=23)

    meals = store.list(DEFAULT_PROFILE_ID, "food", "meal")
    assert len(meals) == 1
    assert meals[0].id == first.id
    assert meals[0].payload["occurred_at"] == first.payload["occurred_at"]
    assert meals[0].payload["ended_at"] == (NOW + timedelta(minutes=40)).isoformat()
    comments = store.list(DEFAULT_PROFILE_ID, "food", "comment")
    assert len(comments) == 2
    assert all(item.payload["meal_id"] == first.id for item in comments)
    attachments = store.list(DEFAULT_PROFILE_ID, "food", "attachment")
    assert len(attachments) == 2
    # Both original binary files are still in the private test vault.
    retained = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert any(p.read_bytes() == b"\xff\xd8\xfftest-photo" for p in retained)
    assert any(p.read_bytes() == gateway.data for p in retained)
    runs_before = len(client.calls)
    service.process_update(comment)
    assert len(client.calls) == runs_before
    assert len(store.list(DEFAULT_PROFILE_ID, "food", "meal")) == 1

    restarted = FoodCoach(PilotStore(clean_database), brain)
    assert not restarted.due(DEFAULT_PROFILE_ID, NOW + timedelta(hours=3, minutes=30))
    assert restarted.due(DEFAULT_PROFILE_ID, NOW + timedelta(hours=4, minutes=10))

    sunday = NOW + timedelta(days=6, hours=6)
    notices = restarted.due(DEFAULT_PROFILE_ID, sunday)
    assert len([notice for notice in notices if notice.key.startswith("food-weekly-")]) == 1
    sent_before = len(gateway.sent)
    assert dispatch_notices(restarted, store, messenger, DEFAULT_PROFILE_ID, "food", sunday) == 1
    assert dispatch_notices(restarted, store, messenger, DEFAULT_PROFILE_ID, "food", sunday) == 0
    assert len(gateway.sent) == sent_before + 1
    assert store.list(DEFAULT_PROFILE_ID, "food", "notice")
