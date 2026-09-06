"""General discussion prompts alongside verified, source-linked lab facts."""

import hashlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.models import Document, LabObservation, ReviewStatus
from health_agent.reminders.time import require_aware_utc
from health_agent.visits.models import Visit, VisitNote
from health_agent.visits.repository import VisitRepository

GENERAL_QUESTIONS = (
    "Какие результаты и жалобы важны для обсуждения на этом приёме?",
    "Что нужно дополнительно проверить и почему?",
    "Как часто нужно приходить на контроль?",
    "Влияют ли тренировки и восстановление на план дальнейших действий?",
    "При каких изменениях следует связаться с врачом раньше?",
)


@dataclass(frozen=True, slots=True)
class VisitLabObservation:
    observation_id: UUID
    document_id: UUID
    observed_on: date
    source_name: str
    source_value: str
    source_unit: str | None
    reference_text: str | None
    page_number: int
    source_reference: str


@dataclass(frozen=True, slots=True)
class VisitBrief:
    visit: Visit
    questions: tuple[VisitNote, ...]
    answers: tuple[VisitNote, ...]
    observations: tuple[VisitLabObservation, ...]
    pending_count: int


def prepare_visit(
    session: Session, profile_id: UUID, code: str, *, now: datetime | None = None
) -> VisitBrief:
    as_of = require_aware_utc(now or datetime.now(UTC))
    repository = VisitRepository(session)
    visit = repository.get(profile_id, code)
    if visit.status != "cancelled":
        for question in GENERAL_QUESTIONS:
            digest = hashlib.sha256(question.encode()).hexdigest()
            repository.add_note(
                profile_id,
                code,
                kind="question",
                text=question,
                action_key=f"visit-prepare:{visit.id}:{digest}",
            )
    notes = repository.notes(profile_id, code)
    observed_on = func.coalesce(Document.collected_date, Document.issued_date)
    rows = session.execute(
        select(LabObservation, observed_on)
        .join(
            Document,
            Document.id == LabObservation.document_id,
        )
        .where(
            Document.profile_id == profile_id,
            LabObservation.status == ReviewStatus.VERIFIED,
            observed_on.is_not(None),
            observed_on <= as_of.date(),
        )
        .order_by(observed_on.desc(), LabObservation.id)
        .limit(10)
    )
    observations = tuple(
        VisitLabObservation(
            observation.id,
            observation.document_id,
            day,
            observation.source_name,
            observation.source_value,
            observation.source_unit,
            observation.reference_text,
            observation.page_number,
            f"document:{observation.document_id}#page={observation.page_number}",
        )
        for observation, day in rows
    )
    pending_count = (
        session.scalar(
            select(func.count())
            .select_from(LabObservation)
            .join(
                Document,
                Document.id == LabObservation.document_id,
            )
            .where(
                Document.profile_id == profile_id,
                LabObservation.status == ReviewStatus.NEEDS_REVIEW,
            )
        )
        or 0
    )
    return VisitBrief(
        visit,
        tuple(note for note in notes if note.kind == "question"),
        tuple(note for note in notes if note.kind == "answer"),
        observations,
        pending_count,
    )
