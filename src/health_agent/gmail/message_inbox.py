"""Content-free Gmail body handoff to the common medical source inbox."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.engine import Engine

from health_agent.db import session_scope
from health_agent.gmail.config import validate_account_id
from health_agent.gmail.types import MessageInboxReceipt, MessageProvenance
from health_agent.models import SourceRecord

_PROVIDER_BY_CLASSIFICATION = {
    "appointment": "gmail_body_appointment",
    "body_medical": "gmail_body_medical",
}


class MedicalMessageInbox:
    """Create idempotent source provenance without storing a message body."""

    def __init__(self, profile_id: str, account_id: str, engine: Engine) -> None:
        self.profile_id = UUID(profile_id)
        self.account_id = validate_account_id(account_id)
        self.engine = engine

    def queue_message(self, provenance: MessageProvenance) -> MessageInboxReceipt:
        self._require_boundary(provenance)
        provider = _PROVIDER_BY_CLASSIFICATION[provenance.classification]
        external_id = f"{self.account_id}:{provenance.message_id}"
        revision = f"message:{provenance.message_id}"
        with session_scope(self.engine) as session:
            record = session.scalar(
                select(SourceRecord).where(
                    SourceRecord.profile_id == self.profile_id,
                    SourceRecord.provider == provider,
                    SourceRecord.external_id == external_id,
                    SourceRecord.revision == revision,
                )
            )
            outcome = "existing"
            if record is None:
                record = SourceRecord(
                    profile_id=self.profile_id,
                    provider=provider,
                    external_id=external_id,
                    revision=revision,
                    source_uri=provenance.source_uri,
                )
                session.add(record)
                session.flush()
                outcome = "queued"
            return MessageInboxReceipt(str(record.id), outcome)

    def _require_boundary(self, provenance: MessageProvenance) -> None:
        if provenance.profile_id != str(self.profile_id):
            raise ValueError("refusing Gmail message for another health profile")
        if provenance.account_id != self.account_id:
            raise ValueError("refusing Gmail message for another Gmail account")
        if provenance.classification not in _PROVIDER_BY_CLASSIFICATION:
            raise ValueError("unsupported Gmail body classification")
