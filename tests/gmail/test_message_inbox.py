from __future__ import annotations

from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from health_agent.gmail.message_inbox import MedicalMessageInbox
from health_agent.gmail.types import MessageProvenance
from health_agent.models import DEFAULT_PROFILE_ID, SourceRecord


def provenance(profile_id: str = str(DEFAULT_PROFILE_ID)) -> MessageProvenance:
    return MessageProvenance(
        profile_id=profile_id,
        account_id="personal",
        account_email="alice@example.com",
        message_id="m1",
        thread_id="t1",
        message_history_id="10",
        internal_date_ms=1000,
        classification="appointment",
        source_uri="https://mail.google.com/mail/#all/m1",
    )


def test_body_message_creates_idempotent_content_free_common_source(
    clean_database: Engine, session: Session
) -> None:
    inbox = MedicalMessageInbox(str(DEFAULT_PROFILE_ID), "personal", clean_database)

    first = inbox.queue_message(provenance())
    second = inbox.queue_message(provenance())

    assert first.outcome == "queued"
    assert second.outcome == "existing"
    assert first.source_record_id == second.source_record_id
    source = session.scalars(select(SourceRecord)).one()
    assert source.provider == "gmail_body_appointment"
    assert source.external_id == "personal:m1"
    assert source.revision == "message:m1"
    assert not hasattr(source, "body")


def test_body_message_rejects_cross_profile_provenance(
    clean_database: Engine,
) -> None:
    inbox = MedicalMessageInbox(str(DEFAULT_PROFILE_ID), "personal", clean_database)
    wrong = provenance("11111111-1111-1111-1111-111111111111")

    try:
        inbox.queue_message(wrong)
    except ValueError as error:
        assert "another health profile" in str(error)
    else:
        raise AssertionError("cross-profile Gmail message was accepted")
