"""Transactional document import and explicit laboratory review actions."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.labs import (
    LabCandidate,
    looks_like_lab_document,
    normalize_lab_result,
    parse_decimal_token,
    parse_lab_candidates,
)
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    DocumentSourceRecord,
    LabObservation,
    ReviewItem,
    ReviewStatus,
    SourceRecord,
    utc_now,
)
from health_agent.pdf import extract_pdf
from health_agent.vault import FileVault

ImportStatus = Literal["imported", "duplicate", "ocr_required", "needs_attention"]
ProcessingStatus = Literal[
    "processed", "needs_review", "ocr_required", "needs_attention"
]


@dataclass(frozen=True, slots=True)
class ImportReport:
    """Safe import result: it intentionally contains no extracted medical text."""

    status: ImportStatus
    processing_status: ProcessingStatus
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

_DATE_TOKEN = r"(\d{4}-\d{2}-\d{2}|\d{1,2}[./]\d{1,2}[./]\d{4})"
_COLLECTION_DATE = re.compile(
    rf"(?:collection|collected|specimen)\s+date\s*[:\-]?\s*{_DATE_TOKEN}|"
    rf"дата\s+(?:забора|взятия)\s*[:\-]?\s*{_DATE_TOKEN}",
    re.IGNORECASE,
)
_ISSUE_DATE = re.compile(
    rf"(?:issue|issued|report)\s+date\s*[:\-]?\s*{_DATE_TOKEN}|"
    rf"дата\s+(?:выдачи|заключения)\s*[:\-]?\s*{_DATE_TOKEN}",
    re.IGNORECASE,
)


def import_document(
    session: Session,
    vault: FileVault,
    source_path: Path,
    source_uri: str | None,
    *,
    profile_id: UUID = DEFAULT_PROFILE_ID,
    source_provider: str = "local_file",
    source_external_id: str | None = None,
    source_revision: str | None = None,
    collected_date: date | None = None,
    issued_date: date | None = None,
) -> ImportReport:
    """Import one PDF as an all-or-nothing database transaction.

    The immutable vault write happens first because it is content-addressed and
    safe to retain if PDF extraction or database persistence later fails.
    """
    source_path = Path(source_path)
    stored_file = vault.store(source_path)
    extracted_pdf = extract_pdf(source_path)
    inferred_collection, inferred_issue = _infer_medical_dates(
        page.text for page in extracted_pdf.pages
    )
    collected_date = collected_date or inferred_collection
    issued_date = issued_date or inferred_issue

    transaction = (
        session.begin_nested() if session.in_transaction() else session.begin()
    )
    with transaction:
        existing = session.scalar(
            select(Document).where(
                Document.profile_id == profile_id,
                Document.sha256 == stored_file.sha256,
            )
        )
        if existing is not None:
            _record_source_occurrence(
                session,
                document=existing,
                provider=source_provider,
                external_id=source_external_id or source_path.name,
                revision=source_revision or f"sha256:{stored_file.sha256}",
                source_uri=source_uri,
            )
            _merge_medical_dates(
                existing, collected_date=collected_date, issued_date=issued_date
            )
            return ImportReport(
                status="duplicate",
                processing_status=cast(ProcessingStatus, existing.processing_status),
                document_id=existing.id,
                candidate_count=0,
                review_count=0,
            )

        candidates = parse_lab_candidates(extracted_pdf.pages)
        lab_like = looks_like_lab_document(extracted_pdf.pages)
        processing_status, safe_error_code = _processing_state(
            extracted_pdf.extraction_method,
            any(page.extraction_method == "ocr_required" for page in extracted_pdf.pages),
            bool(candidates),
            lab_like,
        )
        document = Document(
            profile_id=profile_id,
            sha256=stored_file.sha256,
            vault_path=str(stored_file.path),
            media_type="application/pdf",
            document_type=(
                "laboratory_report" if candidates or lab_like else "unknown_document"
            ),
            collected_date=collected_date,
            issued_date=issued_date,
            processing_status=processing_status,
            safe_error_code=safe_error_code,
        )
        session.add(document)
        session.flush()
        _record_source_occurrence(
            session,
            document=document,
            provider=source_provider,
            external_id=source_external_id or source_path.name,
            revision=source_revision or f"sha256:{stored_file.sha256}",
            source_uri=source_uri,
        )

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

        report_status: ImportStatus = "imported"
        if processing_status == "ocr_required":
            report_status = "ocr_required"
        elif processing_status == "needs_attention":
            report_status = "needs_attention"
        return ImportReport(
            status=report_status,
            processing_status=processing_status,
            document_id=document.id,
            candidate_count=len(observations),
            review_count=len(observations),
        )


def _infer_medical_dates(texts: Iterable[str]) -> tuple[date | None, date | None]:
    text = "\n".join(str(value) for value in texts)
    return _unique_labeled_date(_COLLECTION_DATE, text), _unique_labeled_date(
        _ISSUE_DATE, text
    )


def _unique_labeled_date(pattern: re.Pattern[str], text: str) -> date | None:
    values: set[date] = set()
    for match in pattern.finditer(text):
        raw = next(value for value in match.groups() if value is not None)
        try:
            if "-" in raw:
                values.add(date.fromisoformat(raw))
            else:
                day, month, year = (int(value) for value in re.split(r"[./]", raw))
                values.add(date(year, month, day))
        except ValueError:
            continue
    return next(iter(values)) if len(values) == 1 else None


def approve_observation(
    session: Session,
    observation_id: UUID,
    *,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> None:
    """Approve a pending candidate exactly once."""
    observation = _profile_observation(session, observation_id, profile_id)
    _require_pending(observation)
    review_item = _review_item(observation)
    normalized_value, normalized_unit = normalize_lab_result(
        observation.canonical_name,
        observation.source_value,
        observation.source_unit,
    )
    observation.parsed_value = parse_decimal_token(observation.source_value)
    observation.normalized_value = normalized_value
    observation.normalized_unit = normalized_unit
    observation.status = ReviewStatus.VERIFIED
    review_item.decision = "approved"
    review_item.resolved_at = utc_now()
    _refresh_document_processing_status(session, observation.document_id)
    session.flush()


def reject_observation(
    session: Session,
    observation_id: UUID,
    *,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> None:
    """Reject a pending candidate exactly once."""
    observation = _profile_observation(session, observation_id, profile_id)
    _require_pending(observation)
    review_item = _review_item(observation)
    observation.status = ReviewStatus.REJECTED
    review_item.decision = "rejected"
    review_item.resolved_at = utc_now()
    _refresh_document_processing_status(session, observation.document_id)
    session.flush()


def set_document_medical_dates(
    session: Session,
    document_id: UUID,
    *,
    collected_date: date | None = None,
    issued_date: date | None = None,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> Document:
    """Apply an explicit human-reviewed collection and/or issue date."""
    if collected_date is None and issued_date is None:
        raise ValueError("at least one medical date is required")
    document = session.scalar(
        select(Document)
        .where(
            Document.id == document_id,
            Document.profile_id == profile_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if document is None:
        raise ValueError("document does not exist for this profile")
    if collected_date is not None:
        document.collected_date = collected_date
    if issued_date is not None:
        document.issued_date = issued_date
    if document.safe_error_code == "conflicting_medical_date":
        document.safe_error_code = None
        _refresh_document_processing_status(session, document.id)
    session.flush()
    return document


def correct_observation(
    session: Session,
    observation_id: UUID,
    *,
    source_value: str,
    source_unit: str | None = None,
    canonical_name: str | None = None,
    profile_id: UUID = DEFAULT_PROFILE_ID,
) -> LabObservation:
    """Version a pending observation as a verified human correction.

    The original extracted row remains untouched apart from its review state;
    the corrected values live only in a new, lineage-linked observation.
    """
    original = _profile_observation(session, observation_id, profile_id)
    _require_pending(original)
    review_item = _review_item(original)
    corrected_name = canonical_name or original.canonical_name
    corrected_unit = source_unit if source_unit is not None else original.source_unit
    normalized_value, normalized_unit = normalize_lab_result(
        corrected_name, source_value, corrected_unit
    )
    parsed_value = parse_decimal_token(source_value)
    corrected = LabObservation(
        document_id=original.document_id,
        page_number=original.page_number,
        supersedes_observation_id=original.id,
        canonical_name=corrected_name,
        source_name=original.source_name,
        source_value=source_value,
        parsed_value=parsed_value,
        source_unit=corrected_unit,
        normalized_value=normalized_value,
        normalized_unit=normalized_unit,
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
    _refresh_document_processing_status(session, original.document_id)
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
        source_value=candidate.raw_source_value,
        parsed_value=candidate.parsed_value,
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


def _profile_observation(
    session: Session, observation_id: UUID, profile_id: UUID
) -> LabObservation:
    return session.scalars(
        select(LabObservation)
        .join(LabObservation.document)
        .where(
            LabObservation.id == observation_id,
            Document.profile_id == profile_id,
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    ).one()


def _record_source_occurrence(
    session: Session,
    *,
    document: Document,
    provider: str,
    external_id: str,
    revision: str,
    source_uri: str | None,
) -> None:
    source_record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.profile_id == document.profile_id,
            SourceRecord.provider == provider,
            SourceRecord.external_id == external_id,
            SourceRecord.revision == revision,
        )
    )
    if source_record is None:
        source_record = SourceRecord(
            profile_id=document.profile_id,
            provider=provider,
            external_id=external_id,
            revision=revision,
            source_uri=source_uri,
        )
        session.add(source_record)
        session.flush()
    link = session.get(DocumentSourceRecord, (document.id, source_record.id))
    if link is None:
        session.add(
            DocumentSourceRecord(
                document_id=document.id,
                source_record_id=source_record.id,
                profile_id=document.profile_id,
            )
        )
        session.flush()


def _merge_medical_dates(
    document: Document,
    *,
    collected_date: date | None,
    issued_date: date | None,
) -> None:
    for field, incoming in (
        ("collected_date", collected_date),
        ("issued_date", issued_date),
    ):
        existing = getattr(document, field)
        if existing is None:
            setattr(document, field, incoming)
        elif incoming is not None and existing != incoming:
            document.processing_status = "needs_attention"
            document.safe_error_code = "conflicting_medical_date"


def _processing_state(
    extraction_method: str,
    has_ocr_page: bool,
    has_candidates: bool,
    lab_like: bool,
) -> tuple[ProcessingStatus, str | None]:
    if extraction_method == "ocr_required":
        return "ocr_required", "ocr_required"
    if has_ocr_page:
        return "needs_attention", "partial_ocr_required"
    if has_candidates:
        return "needs_review", None
    if lab_like:
        return "needs_attention", "no_lab_candidates"
    return "processed", None


def _refresh_document_processing_status(
    session: Session, document_id: UUID
) -> None:
    document = session.get_one(Document, document_id)
    if document.safe_error_code is not None:
        return
    pending_observation_id = session.scalar(
        select(LabObservation.id)
        .where(
            LabObservation.document_id == document_id,
            LabObservation.status == ReviewStatus.NEEDS_REVIEW,
        )
        .limit(1)
    )
    document.processing_status = (
        "needs_review" if pending_observation_id is not None else "processed"
    )
