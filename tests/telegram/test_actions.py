from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from health_agent.questions.replies import PrivateReplyStore
from health_agent.telegram.actions import (
    CompositeTelegramTextActions,
    PreparedTelegramTextActions,
)
from health_agent.telegram.service import HELP_TEXT, TelegramUpdateService
from health_agent.telegram.types import MessageContext

CONTEXT = MessageContext(1, UUID(int=1), 10, 10, 2, 3, None, datetime.now(UTC))


def test_composite_stops_at_first_handled_action():
    calls = []

    def handle(context, text):
        calls.append((context, text))
        return "handled" if text == "/review" else None

    actions = CompositeTelegramTextActions((SimpleNamespace(handle=handle),))
    assert actions.handle(CONTEXT, "/review") == "handled"
    assert actions.handle(CONTEXT, "ordinary question") is None
    assert len(calls) == 2


def test_prepared_actions_keep_exact_response_after_restart(tmp_path):
    calls = []

    def handle(context, text):
        calls.append(text)
        return f"Candidate {len(calls)}"

    root = tmp_path / "replies"
    actions = PreparedTelegramTextActions(
        SimpleNamespace(handle=handle), PrivateReplyStore(root)
    )
    first = actions.handle(CONTEXT, "/review")
    reopened = PreparedTelegramTextActions(
        SimpleNamespace(handle=handle), PrivateReplyStore(root)
    )
    assert reopened.handle(CONTEXT, "/review") == first == "Candidate 1"
    assert calls == ["/review"]


def test_unhandled_actions_create_no_reply_file(tmp_path):
    root = tmp_path / "replies"
    actions = PreparedTelegramTextActions(
        SimpleNamespace(handle=lambda *_: None), PrivateReplyStore(root)
    )
    assert actions.handle(CONTEXT, "ordinary question") is None
    assert list(root.iterdir()) == []


def test_text_actions_preserve_existing_routing(tmp_path):
    service = TelegramUpdateService(
        1, SimpleNamespace(), SimpleNamespace(), SimpleNamespace(),
        SimpleNamespace(answer=lambda _: "question"),
        SimpleNamespace(status=lambda _: "status", sync=lambda _: "sync"),
        SimpleNamespace(), staging_root=tmp_path,
        text_actions=SimpleNamespace(
            handle=lambda _, text: "review" if text == "/review" else None
        ),
    )
    assert service._route_text(CONTEXT, "/review") == "review"
    assert service._route_text(CONTEXT, "question") == "question"
    assert service._route_text(CONTEXT, "/status") == "status"
    assert service._route_text(CONTEXT, "/sync") == "sync"
    assert service._route_text(CONTEXT, "/other") == HELP_TEXT
