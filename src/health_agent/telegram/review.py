"""Explicit single-item review, scoped to the bound Telegram document source."""

from __future__ import annotations

import unicodedata
from uuid import UUID

from sqlalchemy import Engine, Select, select
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.importer import (
    approve_observation,
    correct_observation,
    reject_observation,
)
from health_agent.models import (
    Document,
    DocumentSourceRecord,
    LabObservation,
    ReviewStatus,
    SourceRecord,
)
from health_agent.telegram.types import MessageContext

_USAGE = (
    "Формат: /review; /confirm UUID; /correct UUID ЗНАЧЕНИЕ ЕДИНИЦА; /reject UUID. "
    "Решение применяется только к одному показателю явной командой."
)
_COMMANDS = {"/review", "/confirm", "/correct", "/reject"}


class TelegramReviewActions:
    """No free-text mutations; all DB work finishes before sending a reply."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def handle(self, context: MessageContext, text: str) -> str | None:
        parts = text.strip().split(maxsplit=3)
        if not parts or parts[0] not in _COMMANDS:
            return None
        command = parts[0]
        required = 1 if command == "/review" else 4 if command == "/correct" else 2
        if len(text) > 300 or len(parts) != required:
            return _USAGE
        try:
            item_id = UUID(parts[1]) if command != "/review" else None
        except ValueError:
            return _USAGE
        try:
            with session_scope(self._engine) as session:
                if item_id is None:
                    return _next_item(session, context)
                return _decide(session, context, item_id, command, parts)
        except ValueError:
            return (
                "Решение не применено. Проверьте значение и единицу, затем используйте "
                "/review."
            )
        except Exception:  # noqa: BLE001 -- medical data and DB diagnostics are private
            return "Проверка временно недоступна."


def _scoped_items(context: MessageContext) -> Select[tuple[LabObservation]]:
    # The prefix has a terminating delimiter: chat 10 cannot match chat 100.
    occurrence = (
        select(DocumentSourceRecord.document_id)
        .join(SourceRecord, SourceRecord.id == DocumentSourceRecord.source_record_id)
        .where(
            DocumentSourceRecord.document_id == Document.id,
            DocumentSourceRecord.profile_id == context.profile_id,
            SourceRecord.profile_id == context.profile_id,
            SourceRecord.provider == "telegram",
            SourceRecord.external_id.startswith(
                f"telegram:{context.bot_id}:{context.chat_id}:", autoescape=True
            ),
        )
        .exists()
    )
    return (
        select(LabObservation)
        .join(Document, Document.id == LabObservation.document_id)
        .where(Document.profile_id == context.profile_id, occurrence)
    )


def _next_item(session: Session, context: MessageContext) -> str:
    item = session.scalar(
        _scoped_items(context)
        .where(LabObservation.status == ReviewStatus.NEEDS_REVIEW)
        .order_by(Document.created_at, LabObservation.created_at, LabObservation.id)
        .limit(1)
    )
    if item is None:
        return (
            "В этом чате нет извлечённых показателей, ожидающих проверки. Для изображения "
            "всё ещё могут требоваться OCR или локальная проверка; пустая очередь не "
            "означает, что все загрузки распознаны."
        )
    document = item.document
    return (
        f"Непроверенный показатель {item.id}\n"
        f"Источник: документ Telegram {document.id}, страница {item.page_number}.\n"
        f"Извлечено: {_display(item.source_name)} = {_display(item.source_value)} "
        f"{_display(item.source_unit or '(единица не указана)')}\n"
        f"Дата сдачи: {document.collected_date or 'неизвестна'}; "
        f"дата выдачи: {document.issued_date or 'неизвестна'}.\n"
        "Сверьте показатель и медицинскую дату с оригиналом. Если дата отсутствует или "
        "неверна, задайте её локальной командой review set-date, прежде чем полагаться "
        "на этот результат.\n"
        f"/confirm {item.id}\n/correct {item.id} ЗНАЧЕНИЕ ЕДИНИЦА\n/reject {item.id}\n"
        "Только явная команда подтверждает или изменяет показатель. /review показывает "
        "следующий."
    )


def _decide(
    session: Session,
    context: MessageContext,
    item_id: UUID,
    command: str,
    parts: list[str],
) -> str:
    item = session.scalar(
        _scoped_items(context)
        .where(LabObservation.id == item_id)
        # Serialize document status refresh as well as this item's transition.
        .with_for_update(of=(Document, LabObservation))
    )
    if item is None or item.review_item is None:
        return "Этот показатель недоступен для проверки в этом чате."
    decision = {"/confirm": "approved", "/correct": "corrected", "/reject": "rejected"}[
        command
    ]
    reply = {
        "/confirm": f"Показатель {item_id} подтверждён.",
        "/correct": f"Показатель {item_id} исправлен.",
        "/reject": f"Показатель {item_id} отклонён.",
    }[command]
    if item.status is not ReviewStatus.NEEDS_REVIEW:
        same = item.review_item.decision == decision
        if command == "/correct":
            correction = item.review_item.correction_json or {}
            same = (
                same
                and correction.get("source_value") == parts[2]
                and (correction.get("source_unit") == parts[3])
            )
        return (
            reply
            if same
            else "Этот показатель уже обработан; изменения не применены."
        )
    if command == "/confirm":
        approve_observation(session, item_id, profile_id=context.profile_id)
    elif command == "/reject":
        reject_observation(session, item_id, profile_id=context.profile_id)
    else:
        correct_observation(
            session,
            item_id,
            source_value=parts[2],
            source_unit=parts[3],
            profile_id=context.profile_id,
        )
    return reply


def _display(value: str) -> str:
    return "".join(
        character
        for character in value
        if not unicodedata.category(character).startswith("C")
    )[:100]
