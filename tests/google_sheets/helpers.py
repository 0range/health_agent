from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewItem,
    ReviewStatus,
)


def add_profile(session: Session, name: str = "Other") -> UUID:
    profile = Profile(name=name)
    session.add(profile)
    session.flush()
    return profile.id


def add_observation(
    session: Session,
    profile_id: UUID,
    *,
    status: ReviewStatus = ReviewStatus.NEEDS_REVIEW,
    value: str = "12.5",
    source_unit: str = "ng/ml",
    date_value: date | None = date(2026, 8, 20),
    evidence: str = "PRIVATE EVIDENCE",
) -> tuple[LabObservation, ReviewItem]:
    document = Document(
        profile_id=profile_id,
        sha256=uuid4().hex * 2,
        vault_path="/private/vault/document.pdf",
        media_type="application/pdf",
        document_type="laboratory_report",
        collected_date=date_value,
        issued_date=None,
        processing_status="needs_review"
        if status == ReviewStatus.NEEDS_REVIEW
        else "processed",
        safe_error_code=None,
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(
            document_id=document.id,
            page_number=1,
            extracted_text="PRIVATE BODY",
            extraction_method="text",
        )
    )
    observation = LabObservation(
        document_id=document.id,
        page_number=1,
        supersedes_observation_id=None,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value=value,
        parsed_value=Decimal(value),
        source_unit=source_unit,
        normalized_value=Decimal(value) if status == ReviewStatus.VERIFIED else None,
        normalized_unit="ng/mL" if status == ReviewStatus.VERIFIED else None,
        reference_low=Decimal(10),
        reference_high=Decimal(100),
        reference_text="10-100",
        evidence_excerpt=evidence,
        confidence=0.5,
        status=status,
    )
    session.add(observation)
    session.flush()
    review = ReviewItem(observation_id=observation.id, reason_code="parsed_candidate")
    session.add(review)
    session.flush()
    return observation, review
