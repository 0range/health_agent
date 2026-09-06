from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)
from health_agent.visits.models import HealthVisit, HealthVisitNote
from health_agent.visits.preparation import GENERAL_QUESTIONS, prepare_visit
from health_agent.visits.repository import VisitNotFound, VisitRepository

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def document(session, profile=DEFAULT_PROFILE_ID, day=date(2026, 9, 1)):
    doc = Document(
        profile_id=profile,
        sha256=uuid4().hex * 2,
        vault_path="/private/not-for-output",
        media_type="application/pdf",
        document_type="lab",
        collected_date=day,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentPage(document_id=doc.id, page_number=2, extraction_method="fixture")
    )
    session.flush()
    return doc


def lab(session, doc, status=ReviewStatus.VERIFIED, **changes):
    values = {
        "document_id": doc.id,
        "page_number": 2,
        "canonical_name": "glucose",
        "source_name": "Глюкоза",
        "source_value": "95,0",
        "parsed_value": Decimal(95),
        "source_unit": "mg/dL",
        "normalized_value": Decimal("5.27"),
        "normalized_unit": "mmol/L",
        "reference_text": "70–99 mg/dL",
        "evidence_excerpt": "Fixture",
        "confidence": 1,
        "status": status,
    }
    values.update(changes)
    row = LabObservation(**values)
    session.add(row)
    session.flush()
    return row


def visit(repo, **changes):
    values = {
        "title": "Врач",
        "starts_at": NOW,
        "ends_at": NOW + timedelta(hours=1),
        "timezone_name": "UTC",
        "creation_key": "fixture",
    }
    values.update(changes)
    return repo.create(DEFAULT_PROFILE_ID, **values)


def test_preparation_is_idempotent_and_preserves_verified_original_evidence(session):
    other = uuid4()
    session.add(Profile(id=other, name="Other"))
    session.flush()
    own = document(session)
    verified = lab(session, own)
    pending = lab(session, own, ReviewStatus.NEEDS_REVIEW, source_value="777")
    lab(session, document(session, other), source_value="888")
    lab(session, document(session, day=None), source_value="999")
    lab(session, document(session, day=date(2027, 1, 1)), source_value="123")
    repo = VisitRepository(session)
    created = visit(repo, source_document_id=own.id)
    first = prepare_visit(session, DEFAULT_PROFILE_ID, created.public_code, now=NOW)
    second = prepare_visit(session, DEFAULT_PROFILE_ID, created.public_code, now=NOW)
    assert first == second
    assert {note.text for note in first.questions} == set(GENERAL_QUESTIONS)
    assert len(repo.notes(DEFAULT_PROFILE_ID, created.public_code)) == 5
    assert first.pending_count == 1
    assert len(first.observations) == 1
    fact = first.observations[0]
    assert fact.observation_id == verified.id
    assert (fact.source_value, fact.source_unit, fact.reference_text) == (
        "95,0",
        "mg/dL",
        "70–99 mg/dL",
    )
    assert fact.observed_on == date(2026, 9, 1)
    assert fact.source_reference == f"document:{own.id}#page=2"
    assert pending.status is ReviewStatus.NEEDS_REVIEW
    with pytest.raises(VisitNotFound):
        prepare_visit(session, other, created.public_code, now=NOW)


def test_preparation_caps_labs_and_cancelled_visit_is_readable(session):
    doc = document(session)
    for _ in range(12):
        lab(session, doc)
    repo = VisitRepository(session)
    created = visit(repo)
    brief = prepare_visit(session, DEFAULT_PROFILE_ID, created.public_code, now=NOW)
    assert len(brief.observations) == 10
    repo.cancel(DEFAULT_PROFILE_ID, created.public_code)
    assert (
        len(
            prepare_visit(
                session, DEFAULT_PROFILE_ID, created.public_code, now=NOW
            ).questions
        )
        == 5
    )


def test_source_document_and_note_foreign_keys_enforce_profile(session):
    other = uuid4()
    session.add(Profile(id=other, name="Other"))
    session.flush()
    foreign = document(session, other)
    repo = VisitRepository(session)
    with pytest.raises(VisitNotFound):
        visit(repo, source_document_id=foreign.id)
    own_visit = visit(repo)
    with pytest.raises(IntegrityError), session.begin_nested():
        row = session.scalar(select(HealthVisit).where(HealthVisit.id == own_visit.id))
        row.source_document_id = foreign.id
        session.flush()
    with pytest.raises(IntegrityError), session.begin_nested():
        session.add(
            HealthVisitNote(
                visit_id=own_visit.id,
                profile_id=other,
                kind="answer",
                text="Foreign",
                action_key="wrong",
            )
        )
        session.flush()


def test_note_action_key_cannot_be_reused_on_different_visit_or_profile(session):
    repo = VisitRepository(session)
    first = visit(repo)
    second = visit(repo, creation_key="second")
    repo.add_note(
        DEFAULT_PROFILE_ID,
        first.public_code,
        kind="answer",
        text="x",
        action_key="same",
    )
    with pytest.raises(ValueError):
        repo.add_note(
            DEFAULT_PROFILE_ID,
            second.public_code,
            kind="answer",
            text="x",
            action_key="same",
        )
    other = uuid4()
    session.add(Profile(id=other, name="Other"))
    session.flush()
    third = repo.create(
        other,
        title="Other",
        starts_at=NOW,
        ends_at=NOW + timedelta(hours=1),
        timezone_name="UTC",
        creation_key="third",
    )
    with pytest.raises(ValueError):
        repo.add_note(
            other, third.public_code, kind="answer", text="x", action_key="same"
        )
