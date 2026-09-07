"""Adapters that run each coach through the established Telegram delivery path."""

import json
import os
import tempfile
import time
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from health_agent.automation.storage import GlobalRunLock, private_directory
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.google_calendar.composition import build_publication_service
from health_agent.pilot.brain import PilotBrain
from health_agent.pilot.contracts import Attachment, Coach, Store
from health_agent.pilot.goals import goal_action
from health_agent.pilot.storage import PilotStore
from health_agent.questions.composition import (
    ReadOnlyQuestionCommands,
    TelegramMedicalInbox,
    question_status,
)
from health_agent.questions.context import HealthContextBuilder
from health_agent.questions.openai import build_responder_input
from health_agent.questions.replies import PrivateReplyStore
from health_agent.questions.safety import guard_urgent_question
from health_agent.reminders.telegram import DatabaseReminderCommands
from health_agent.telegram.actions import (
    CompositeTelegramTextActions,
    PreparedTelegramTextActions,
)
from health_agent.telegram.api import TelegramAPIError, TelegramBotAPI
from health_agent.telegram.messenger import TelegramMessenger
from health_agent.telegram.review import TelegramReviewActions
from health_agent.telegram.service import TelegramLongPoller, TelegramUpdateService
from health_agent.telegram.stores import PrivateBotTokenStore, SqliteTelegramState
from health_agent.telegram.types import (
    AttachmentProvenance,
    HealthQuestion,
    InboxReceipt,
    MessageContext,
)
from health_agent.vault import FileVault
from health_agent.visits.telegram import DatabaseVisitCommands

HELP = {
    "sleep": "Помогаю со сном и помню наши обсуждения.\nУтром напиши, как себя чувствуешь, или /сон и заметку. Можно голосом.\n/дневник — записи\n/итоги — разбор недели\n/утро 09:00 — время вопроса\n/утро выкл — отключить\n/цели — твои цели\nМедицинские PDF можно присылать сюда, как раньше.",
    "food": "Присылай фото еды с подписью или /ел 12:00 обед. Сохраню приём, оценю тарелку и напомню о следующем.\n/время 12:30 — исправить время\n/позже 30 — отложить\n/пропустить — пропустить напоминание\n/сегодня · /неделя — дневник\n/напоминания выкл — пауза\n/цели — цели",
    "training": "Здесь годовые ориентиры, план недели и разбор тренировок.\n/год — ориентиры года\n/план — предложить неделю\n/сохранить план — принять предложение\n/итоги — план и факт\n/цели — цели\nПланы остаются здесь, во внешние системы ничего не записываю.",
}


@contextmanager
def _pilot_lock(path: Path):
    lock = GlobalRunLock(path)
    if not lock.acquire():
        raise ValueError("pilot_already_running")
    try:
        yield
    finally:
        lock.release()


class PilotActions:
    def __init__(self, coach: Coach, store: Store, domain: str) -> None:
        self.coach, self.store, self.domain = coach, store, domain

    def handle(self, context: MessageContext, text: str) -> str:
        key = f"telegram:{context.bot_id}:{context.update_id}"
        now = context.sent_at or context.received_at
        source = self.store.put(
            context.profile_id,
            self.domain,
            "input",
            key,
            {
                "text": text,
                "sent_at": context.sent_at.isoformat() if context.sent_at else None,
                "received_at": context.received_at.isoformat(),
                "time_source": "telegram" if context.sent_at else "received_fallback",
            },
            at=now,
        )
        text, now = source.payload["text"], source.at
        urgent = guard_urgent_question(text)
        if urgent is not None:
            return urgent
        reply = goal_action(
            self.store, context.profile_id, self.domain, text, source_key=key, now=now
        )
        if reply is not None:
            return reply
        return self.coach.handle(context.profile_id, text, source_key=key, now=now)


class PilotQuestions:
    def __init__(self, actions: PilotActions, replies: PrivateReplyStore) -> None:
        self.actions, self.replies = actions, replies

    def answer(self, question: HealthQuestion) -> str:
        return self.actions.handle(question.context, question.text)

    def complete_update(self, bot_id: int, update_id: int) -> None:
        self.replies.complete(bot_id, update_id)


class PilotInbox:
    def __init__(
        self,
        coach: Coach,
        store: Store,
        domain: str,
        root: Path,
        brain: PilotBrain,
        medical: TelegramMedicalInbox | None = None,
    ) -> None:
        self.coach, self.store, self.domain = coach, store, domain
        self.root, self.brain, self.medical = root, brain, medical

    def ingest(
        self, provenance: AttachmentProvenance, chunks: Iterable[bytes]
    ) -> InboxReceipt:
        if self.medical is not None and provenance.kind == "document":
            return self.medical.ingest(provenance, chunks)
        staging = private_directory(self.root / "staging")
        descriptor, temporary = tempfile.mkstemp(dir=staging)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                for chunk in chunks:
                    handle.write(chunk)
            saved = FileVault(
                self.root / "vault" / str(provenance.context.profile_id)
            ).store(temporary_path)
        finally:
            temporary_path.unlink(missing_ok=True)
        now = provenance.context.sent_at or provenance.context.received_at
        record = self.store.put(
            provenance.context.profile_id,
            self.domain,
            "attachment",
            provenance.source_external_id,
            {
                "path": str(saved.path),
                "sha256": saved.sha256,
                "media_type": provenance.validated_media_type,
                "caption": provenance.caption,
                "source": "telegram",
                "status": "received",
            },
            at=now,
        )
        if record.payload.get("reply"):
            return InboxReceipt(
                saved.sha256, saved.size_bytes, "received", record.payload["reply"]
            )
        text = provenance.caption
        media_type = provenance.validated_media_type or "application/octet-stream"
        attachment: Attachment | None = Attachment(saved.path, media_type, text)
        if provenance.kind == "voice":
            try:
                if (
                    provenance.duration_seconds is not None
                    and provenance.duration_seconds > 30
                ):
                    raise ValueError("voice_duration_limit")
                text = self.brain.transcribe(saved.path)
                self.store.patch(
                    provenance.context.profile_id,
                    record.id,
                    {**record.payload, "transcript": text, "status": "transcribed"},
                )
                attachment = None
            except Exception:  # noqa: BLE001 -- audio remains durable; credentials/errors stay private
                reply = "Голосовое сохранил, но расшифровать пока не удалось. Напиши коротко текстом; запись не потеряна."
                self.store.patch(
                    provenance.context.profile_id,
                    record.id,
                    {
                        **record.payload,
                        "status": "transcription_pending",
                        "reply": reply,
                    },
                )
                return InboxReceipt(saved.sha256, saved.size_bytes, "received", reply)
        try:
            reply = guard_urgent_question(text) or self.coach.handle(
                provenance.context.profile_id,
                text,
                source_key=provenance.source_external_id,
                now=now,
                attachment=attachment,
            )
        except Exception:  # noqa: BLE001 -- preserve original through model/adapter failure
            reply = "Файл сохранён, но обработка пока не завершилась. Можно продолжить сообщением."
        latest = self.store.get(provenance.context.profile_id, record.id) or record
        self.store.patch(
            provenance.context.profile_id, record.id, {**latest.payload, "reply": reply}
        )
        return InboxReceipt(saved.sha256, saved.size_bytes, "received", reply)


def dispatch_notices(
    coach: Coach,
    store: Store,
    messenger: TelegramMessenger,
    profile_id: UUID,
    domain: str,
    now: datetime,
) -> int:
    sent = 0
    for notice in coach.due(profile_id, now):
        frozen = store.put(
            profile_id, domain, "outbound", notice.key, {"text": notice.text}, at=now
        )
        messenger.send_to_profile(
            profile_id,
            frozen.payload["text"],
            delivery_key=f"pilot:{domain}:{notice.key}",
        )
        store.put(
            profile_id,
            domain,
            "notice",
            notice.key,
            {"text": frozen.payload["text"], "delivered_at": now.isoformat()},
            at=now,
        )
        sent += 1
    return sent


def telegram_root(settings: Settings, domain: str) -> Path:
    return (
        settings.telegram_root
        if domain == "sleep"
        else Path("data/pilot") / domain / "telegram"
    )


def build_coach(
    settings: Settings, store: PilotStore, profile_id: UUID, domain: str
) -> tuple[Coach, PilotBrain]:
    brain = PilotBrain(settings, profile_id, store=store, domain=domain)
    if domain == "sleep":
        from health_agent.pilot.sleep import SleepCoach

        def health_context(profile: UUID, question: str) -> dict[str, Any]:
            with session_scope(store.engine) as session:
                context = HealthContextBuilder(session).build(profile, question)
                blocks: Any = build_responder_input(question, context)[0]["content"]
                return json.loads(blocks[1]["text"])

        return SleepCoach(store, brain, health_context=health_context), brain
    if domain == "food":
        from health_agent.pilot.food import FoodCoach

        return FoodCoach(store, brain), brain
    if domain == "training":
        from health_agent.pilot.coros import CorosHTTPTransport, CorosReadClient
        from health_agent.pilot.coros_auth import CorosOAuth
        from health_agent.pilot.training import TrainingCoach

        auth = CorosOAuth(Path("data/pilot/training/coros") / str(profile_id))
        source = CorosReadClient(CorosHTTPTransport(auth)) if auth.status() else None
        return TrainingCoach(store, brain, activity_source=source), brain
    raise ValueError("unknown_pilot_domain")


def run_pilot(settings: Settings, domain: str, profile_id: UUID) -> None:
    if domain not in HELP:
        raise ValueError("unknown_pilot_domain")
    root = telegram_root(settings, domain)
    private_directory(root)
    with _pilot_lock(root / "pilot.lock"):
        tokens = PrivateBotTokenStore(
            settings.effective_telegram_token_file
            if domain == "sleep"
            else root / "bot-token"
        )
        credential = tokens.load_verified()
        state = SqliteTelegramState(
            settings.telegram_state_file
            if domain == "sleep"
            else root / "state.sqlite3"
        )
        if state.identity_for_profile(credential.bot_id, profile_id) is None:
            raise ValueError("pilot_identity_not_bound")
        engine = build_engine(settings)
        store = PilotStore(engine)
        coach, brain = build_coach(settings, store, profile_id, domain)
        gateway = TelegramBotAPI(credential.token)
        messenger = TelegramMessenger(credential.bot_id, gateway, state)
        replies = PrivateReplyStore(root / "prepared-replies")
        actions = PilotActions(coach, store, domain)
        handlers: Any = actions
        medical = None
        if domain == "sleep":
            handlers = CompositeTelegramTextActions(
                (
                    TelegramReviewActions(engine),
                    DatabaseVisitCommands(
                        engine, build_publication_service(settings, engine)
                    ),
                    DatabaseReminderCommands(engine),
                    actions,
                )
            )
            medical = TelegramMedicalInbox(
                engine, FileVault(settings.vault_root), root / "medical-staging"
            )
        inbox = PilotInbox(coach, store, domain, root, brain, medical)
        updates = TelegramUpdateService(
            credential.bot_id,
            gateway,
            state,
            messenger,
            PilotQuestions(actions, replies),
            ReadOnlyQuestionCommands(
                lambda profile: question_status(settings, profile)
            ),
            inbox,
            staging_root=root / "staging",
            text_actions=PreparedTelegramTextActions(handlers, replies),
            help_text=HELP[domain],
        )
        poller = TelegramLongPoller(
            credential.bot_id, gateway, state, updates, timeout_seconds=10
        )
        poller.validate_startup()
        try:
            while True:
                try:
                    report = poller.poll_once()
                    now = datetime.now(UTC)
                    dispatch_notices(coach, store, messenger, profile_id, domain, now)
                    if report.blocked_until is not None:
                        time.sleep(
                            min(
                                10,
                                max(0.1, (report.blocked_until - now).total_seconds()),
                            )
                        )
                except TelegramAPIError as error:
                    state.record_poll(credential.bot_id, error.safe_error_code)
                    time.sleep(5)
                except Exception as error:  # noqa: BLE001 -- supervisor survives isolated domain failures
                    state.record_poll(
                        credential.bot_id, f"pilot_{type(error).__name__}"
                    )
                    time.sleep(5)
        finally:
            engine.dispose()
