"""Synthetic production composition, import/review/delivery/restart integration."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pymupdf
from sqlalchemy import select

from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.models import Document, LabObservation, ReviewStatus
from health_agent.questions.composition import (
    DatabaseHealthContextBuilder,
    QuestionStatus,
    build_telegram_question_runtime,
)
from health_agent.questions.service import HealthQuestionApplicationService
from health_agent.telegram.api import TelegramDeferred
from health_agent.telegram.stores import SqliteTelegramState
from health_agent.telegram.types import (
    RemoteFile,
    TelegramIdentity,
    VerifiedBotCredential,
)

PROFILE_ID = UUID(int=1)


def test_production_photo_review_confirm_429_restart_question(
    clean_database, tmp_path, monkeypatch
):
    now = datetime.now(UTC)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=300, height=100)
        payload = page.get_pixmap().tobytes("jpeg")
    monkeypatch.setattr(
        "health_agent.images.recognize_image",
        lambda _: f"Collection date: {now.date()}\nFerritin 42 ng/mL 30-400",
    )
    settings = Settings(
        telegram_root=tmp_path / "telegram",
        temporary_root=tmp_path / "temporary",
        vault_root=tmp_path / "vault",
    )
    state = SqliteTelegramState(settings.telegram_state_file, clock=lambda: now)
    state.register_bot(77, "synthetic")
    state.bind_identity(77, TelegramIdentity(10, PROFILE_ID, 10))
    sent, attempted, contexts = [], [], []
    defer = False

    def send(chat_id, text):
        nonlocal defer
        attempted.append(text)
        if defer:
            defer = False
            raise TelegramDeferred(now + timedelta(seconds=1))
        sent.append((chat_id, text))
        return len(sent)

    gateway = SimpleNamespace(
        send_message=send,
        get_file=lambda file_id: RemoteFile(file_id, "unique", "file", len(payload)),
        download_chunks=lambda _: iter((payload,)),
    )

    def answer(*, question, context, profile_id, request_id=None):
        contexts.append(context)
        return "Recorded ferritin is 42 ng/mL [LAB1]."

    application = HealthQuestionApplicationService(
        DatabaseHealthContextBuilder(clean_database), SimpleNamespace(respond=answer)
    )

    def service():
        runtime = build_telegram_question_runtime(
            settings,
            question_application_factory=lambda _: application,
            token_store_factory=lambda _: SimpleNamespace(
                load_verified=lambda: VerifiedBotCredential("fake", 77, "synthetic")
            ),
            state_factory=lambda _: SqliteTelegramState(
                settings.telegram_state_file, clock=lambda: now
            ),
            gateway_factory=lambda _: gateway,
            engine_factory=lambda _: clean_database,
            status_reader=lambda _: QuestionStatus(True, {}),
            poller_factory=lambda _bot, _gateway, _state, updates: SimpleNamespace(
                updates=updates
            ),
        )
        return runtime.poller.updates

    def update(number, text=None):
        message = {
            "message_id": number,
            "from": {"id": 10, "is_bot": False},
            "chat": {"id": 10, "type": "private"},
        }
        if text is None:
            message["photo"] = [
                {
                    "file_id": "image",
                    "file_unique_id": "unique",
                    "file_size": len(payload),
                    "width": 300,
                    "height": 100,
                }
            ]
        else:
            message["text"] = text
        return {"update_id": number, "message": message}

    assert service().process_update(update(1)).status == "received"
    with session_scope(clean_database) as session:
        item = session.scalars(select(LabObservation)).one()
        item_id = item.id
        assert item.status is ReviewStatus.NEEDS_REVIEW
        document = session.scalars(select(Document)).one()
        assert document.media_type == "image/jpeg"
        assert Path(document.vault_path).read_bytes() == payload
    assert service().process_update(update(2, "/review")).status == "replied"
    assert str(item_id) in sent[-1][1]
    defer = True
    confirmation = update(3, f"/confirm {item_id}")
    assert service().process_update(confirmation).status == "retryable_error"
    now += timedelta(seconds=2)
    assert service().process_update(confirmation).status == "replied"
    assert attempted[-1] == attempted[-2] == f"Confirmed item {item_id}."
    sent_count = len(sent)
    assert service().process_update(confirmation).terminal
    assert len(sent) == sent_count
    assert (
        service().process_update(update(4, "What is my ferritin?")).status == "replied"
    )
    assert "[LAB1]" in sent[-1][1]
    assert len(contexts) == 1 and len(contexts[0].evidence) == 1
    assert contexts[0].evidence[0].metric == "ferritin"
    assert contexts[0].evidence[0].value == "42"
    assert list(settings.telegram_staging_root.iterdir()) == []
    assert list(settings.temporary_root.iterdir()) == []
    assert list((settings.telegram_root / "prepared-replies").iterdir()) == []
