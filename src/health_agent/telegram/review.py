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
    "Usage: /review; /confirm ITEM_UUID; /correct ITEM_UUID VALUE UNIT; "
    "/reject ITEM_UUID. These explicit commands apply your decision to one item."
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
            return "Decision not applied. Check the value/unit and use /review."
        except Exception:  # noqa: BLE001 -- medical data and DB diagnostics are private
            return "Review is temporarily unavailable."


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
            "No pending extracted items in this chat. An image may still need OCR "
            "or local attention; an empty queue does not mean every upload was read."
        )
    document = item.document
    return (
        f"Unverified item {item.id}\n"
        f"Source: Telegram document {document.id}, page {item.page_number}.\n"
        f"Extracted: {_display(item.source_name)} = {_display(item.source_value)} "
        f"{_display(item.source_unit or '(unit missing)')}\n"
        f"Collection date: {document.collected_date or 'unknown'}; "
        f"issue date: {document.issued_date or 'unknown'}.\n"
        "Compare with the original, including the medical date. Missing/wrong dates "
        "need local review set-date before relying on this result.\n"
        f"/confirm {item.id}\n/correct {item.id} VALUE UNIT\n/reject {item.id}\n"
        "Only an explicit command confirms or changes this item. /review shows the next item."
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
        return "This review item is unavailable in this chat."
    decision = {"/confirm": "approved", "/correct": "corrected", "/reject": "rejected"}[
        command
    ]
    reply = {
        "/confirm": f"Confirmed item {item_id}.",
        "/correct": f"Corrected item {item_id}.",
        "/reject": f"Rejected item {item_id}.",
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
            reply if same else "This item is already resolved; no changes were applied."
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
