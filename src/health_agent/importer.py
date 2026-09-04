"""Transactional document import and explicit laboratory review actions."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.labs import LabCandidate, parse_lab_candidates
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    ReviewItem,
    ReviewStatus,
    SourceRecord,
    utc_now,
)
from health_agent.pdf import extract_pdf
from health_agent.vault import FileVault


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Safe import result: it intentionally contains no extracted medical text."""

    status: Literal["imported", "duplicate"]
    document_id: UUID
    candidate_count: int
    review_count: int


class InvalidReviewTransition(ValueError):
    """Raised when an already-resolved observation is reviewed again."""

    def __init__(self, status: ReviewStatus) -> None:
        super().__init__(f"Cannot review an observation with status {status.value!r}")
        self.status = status


_CANONICAL_NAMES = {
    "ferritin": "ferritin",
    "ферритин": "ferritin",
    "b12": "vitamin_b12",
    "витамин b12": "vitamin_b12",
    "vitamin b12": "vitamin_b12",
    "кобаламин": "vitamin_b12",
    "фолат": "folate",
    "фолиевая кислота": "folate",
    "витамин b9": "folate",
    "folate": "folate",
    "folic acid": "folate",
    "vitamin b9": "folate",
    "b9": "folate",
    "холестерин общий": "total_cholesterol",
    "общий холестерин": "total_cholesterol",
    "total cholesterol": "total_cholesterol",
    "cholesterol": "total_cholesterol",
    "холестерин лпнп": "ldl_cholesterol",
    "лпнп": "ldl_cholesterol",
    "ldl cholesterol": "ldl_cholesterol",
    "ldl": "ldl_cholesterol",
    "холестерин лпвп": "hdl_cholesterol",
    "лпвп": "hdl_cholesterol",
    "hdl cholesterol": "hdl_cholesterol",
    "hdl": "hdl_cholesterol",
    "триглицериды": "triglycerides",
    "triglycerides": "triglycerides",
    "железо": "iron",
    "iron": "iron",
    "витамин d": "vitamin_d",
    "vitamin d": "vitamin_d",
    "пролактин": "prolactin",
    "prolactin": "prolactin",
}


def import_document(
    session: Session,
    vault: FileVault,
    source_path: Path,
    source_uri: str | None,
) -> ImportReport:
    """Import one PDF as an all-or-nothing database transaction.

    The immutable vault write happens first because it is content-addressed and
    safe to retain if PDF extraction or database persistence later fails.
    """
    source_path = Path(source_path)
    stored_file = vault.store(source_path)

    transaction = (
        session.begin_nested() if session.in_transaction() else session.begin()
    )
    with transaction:
        existing = session.scalar(
            select(Document).where(Document.sha256 == stored_file.sha256)
        )
        if existing is not None:
            return ImportReport(
                status="duplicate",
                document_id=existing.id,
                candidate_count=0,
                review_count=0,
            )

        extracted_pdf = extract_pdf(source_path)
        candidates = parse_lab_candidates(extracted_pdf.pages)
        source_record = SourceRecord(
            provider="local_file",
            external_id=source_path.name,
            revision=f"sha256:{stored_file.sha256}",
            source_uri=source_uri,
        )
        document = Document(
            source_record=source_record,
            sha256=stored_file.sha256,
            vault_path=str(stored_file.path),
            media_type="application/pdf",
            document_type="laboratory_report",
            processing_status="needs_review" if candidates else "processed",
        )
        session.add(document)
        session.flush()

        session.add_all(
            DocumentPage(
                document_id=document.id,
                page_number=page.page_number,
                extracted_text=page.text or None,
                extraction_method=page.extraction_method,
            )
            for page in extracted_pdf.pages
        )
        session.flush()

        observations = [
            _observation_from_candidate(document.id, candidate)
            for candidate in candidates
        ]
        session.add_all(observations)
        session.flush()
        session.add_all(
            ReviewItem(observation=observation, reason_code="parsed_candidate")
            for observation in observations
        )

        return ImportReport(
            status="imported",
            document_id=document.id,
            candidate_count=len(observations),
            review_count=len(observations),
        )


def approve_observation(session: Session, observation_id: UUID) -> None:
    """Approve a pending candidate exactly once."""
    observation = session.get_one(LabObservation, observation_id)
    _require_pending(observation)
    review_item = _review_item(observation)
    observation.status = ReviewStatus.VERIFIED
    review_item.decision = "approved"
    review_item.resolved_at = utc_now()
    session.flush()


def reject_observation(session: Session, observation_id: UUID) -> None:
    """Reject a pending candidate exactly once."""
    observation = session.get_one(LabObservation, observation_id)
    _require_pending(observation)
    review_item = _review_item(observation)
    observation.status = ReviewStatus.REJECTED
    review_item.decision = "rejected"
    review_item.resolved_at = utc_now()
    session.flush()


def correct_observation(
    session: Session,
    observation_id: UUID,
    *,
    source_value: str,
    source_unit: str | None = None,
    canonical_name: str | None = None,
) -> LabObservation:
    """Version a pending observation as a verified human correction.

    The original extracted row remains untouched apart from its review state;
    the corrected values live only in a new, lineage-linked observation.
    """
    original = session.get_one(LabObservation, observation_id)
    _require_pending(original)
    review_item = _review_item(original)
    corrected = LabObservation(
        document_id=original.document_id,
        page_number=original.page_number,
        supersedes_observation_id=original.id,
        canonical_name=canonical_name or original.canonical_name,
        source_name=original.source_name,
        source_value=source_value,
        source_unit=source_unit if source_unit is not None else original.source_unit,
        normalized_value=None,
        normalized_unit=None,
        reference_low=original.reference_low,
        reference_high=original.reference_high,
        reference_text=original.reference_text,
        evidence_excerpt=original.evidence_excerpt,
        confidence=original.confidence,
        status=ReviewStatus.VERIFIED,
    )
    original.status = ReviewStatus.REJECTED
    review_item.decision = "corrected"
    review_item.correction_json = {
        "canonical_name": corrected.canonical_name,
        "source_unit": corrected.source_unit,
        "source_value": corrected.source_value,
    }
    review_item.resolved_at = utc_now()
    session.add(corrected)
    session.flush()
    return corrected


def _observation_from_candidate(
    document_id: UUID, candidate: LabCandidate
) -> LabObservation:
    return LabObservation(
        document_id=document_id,
        page_number=candidate.page_number,
        canonical_name=_canonical_name(candidate.source_name),
        source_name=candidate.source_name,
        source_value=str(candidate.source_value),
        source_unit=candidate.unit,
        normalized_value=None,
        normalized_unit=None,
        reference_low=None,
        reference_high=None,
        reference_text=candidate.reference_text,
        evidence_excerpt=candidate.evidence_excerpt,
        confidence=0.5,
        status=ReviewStatus.NEEDS_REVIEW,
    )


def _canonical_name(source_name: str) -> str:
    normalised = " ".join(source_name.casefold().split())
    return _CANONICAL_NAMES[normalised]


def _require_pending(observation: LabObservation) -> None:
    if observation.status is not ReviewStatus.NEEDS_REVIEW:
        raise InvalidReviewTransition(observation.status)


def _review_item(observation: LabObservation) -> ReviewItem:
    if observation.review_item is None:
        raise RuntimeError("Pending observations must have a review item")
    return observation.review_item
