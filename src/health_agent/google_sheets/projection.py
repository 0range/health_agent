"""Privacy-bounded deterministic spreadsheet projections."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.google_sheets.models import SheetsReviewDecisionAudit
from health_agent.google_sheets.types import (
    ManagedSheet,
    SheetValue,
    WorkbookBinding,
    WorkbookProjection,
)
from health_agent.models import (
    Document,
    DocumentSourceRecord,
    LabObservation,
    ReviewItem,
    ReviewStatus,
    SourceRecord,
)

LAB_HEADERS = (
    "Medical date",
    "Analyte",
    "Source name",
    "Normalized value",
    "Normalized unit",
    "Source value",
    "Source unit",
    "Reference",
    "Document ID",
    "Source link",
)
REVIEW_HEADERS = (
    "Review item ID",
    "Observation ID",
    "Profile ID",
    "Row version",
    "Analyte",
    "Value",
    "Unit",
    "Reference",
    "Medical date",
    "Confidence",
    "Reason",
    "Source link",
    "Decision",
    "Corrected value",
    "Corrected unit",
    "Corrected canonical name",
)
SOURCE_HEADERS = (
    "Source",
    "Account",
    "Authorization",
    "Last attempt",
    "Last success",
    "Retry at",
    "Safe error",
    "Freshness",
)
_GOOGLE_RESOURCE_ID = re.compile(r"[A-Za-z0-9_-]{8,300}")


@dataclass(frozen=True, slots=True)
class SourceStatusRow:
    source: str
    account: str
    authorization: str
    last_attempt: str | None = None
    last_success: str | None = None
    retry_at: str | None = None
    safe_error: str | None = None
    freshness: str = "unknown"

    def values(self) -> tuple[SheetValue, ...]:
        return (
            self.source,
            self.account,
            self.authorization,
            self.last_attempt,
            self.last_success,
            self.retry_at,
            self.safe_error,
            self.freshness,
        )


@dataclass(frozen=True, slots=True)
class ExpectedReviewRow:
    review_item_id: UUID
    observation_id: UUID
    profile_id: UUID
    row_version: str
    immutable_values: tuple[SheetValue, ...]
    sheet_row: int

    def values(self) -> tuple[SheetValue, ...]:
        return self.immutable_values + ("", "", "", "")


@dataclass(frozen=True, slots=True)
class ProjectionBundle:
    workbook: WorkbookProjection
    pending_reviews: tuple[ExpectedReviewRow, ...]
    known_reviews: tuple[ExpectedReviewRow, ...]


def _render(value: object) -> SheetValue:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _medical_date(document: Document) -> date | None:
    return document.collected_date or document.issued_date


def _render_medical_date(document: Document) -> str:
    medical_date = _medical_date(document)
    return medical_date.isoformat() if medical_date is not None else "missing"


def _source_link(session: Session, document_id: UUID) -> str | None:
    candidates = session.scalars(
        select(SourceRecord.source_uri)
        .join(
            DocumentSourceRecord,
            DocumentSourceRecord.source_record_id == SourceRecord.id,
        )
        .where(DocumentSourceRecord.document_id == document_id)
        .where(SourceRecord.provider == "google_drive")
        .where(SourceRecord.source_uri.is_not(None))
        .order_by(SourceRecord.received_at, SourceRecord.id)
    )
    for candidate in candidates:
        if candidate is None:
            continue
        canonical = _canonical_google_source_link(candidate)
        if canonical is not None:
            return canonical
    return None


def _canonical_google_source_link(candidate: str) -> str | None:
    parsed = urlsplit(candidate.strip())
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
    ):
        return None
    parts = tuple(part for part in parsed.path.split("/") if part)
    resource_id: str | None = None
    canonical_parts: tuple[str, ...] | None = None
    if parsed.hostname == "drive.google.com" and len(parts) >= 3:
        if parts[:2] == ("file", "d"):
            resource_id = parts[2]
            canonical_parts = ("file", "d", resource_id, "view")
        elif parts[:2] == ("drive", "folders"):
            resource_id = parts[2]
            canonical_parts = ("drive", "folders", resource_id)
    elif (
        parsed.hostname == "docs.google.com"
        and len(parts) >= 3
        and parts[0] in {"document", "spreadsheets", "presentation", "forms"}
        and parts[1] == "d"
    ):
        resource_id = parts[2]
        canonical_parts = (parts[0], "d", resource_id, "edit")
    if (
        resource_id is None
        or canonical_parts is None
        or _GOOGLE_RESOURCE_ID.fullmatch(resource_id) is None
    ):
        return None
    return urlunsplit(
        ("https", parsed.hostname or "", "/" + "/".join(canonical_parts), "", "")
    )


def _row_version(values: tuple[SheetValue, ...]) -> str:
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _expected_review(
    session: Session,
    observation: LabObservation,
    review: ReviewItem,
    profile_id: UUID,
    row_number: int,
    audited_version: str | None = None,
) -> ExpectedReviewRow:
    medical_date = _medical_date(observation.document)
    partial: tuple[SheetValue, ...] = (
        str(review.id),
        str(observation.id),
        str(profile_id),
        "",
        observation.canonical_name,
        observation.source_value,
        observation.source_unit,
        observation.reference_text,
        medical_date.isoformat() if medical_date is not None else "missing",
        str(observation.confidence),
        review.reason_code,
        _source_link(session, observation.document_id),
    )
    version = audited_version or _row_version(
        partial[:3] + partial[4:] + (ReviewStatus.NEEDS_REVIEW.value,)
    )
    values = partial[:3] + (version,) + partial[4:]
    return ExpectedReviewRow(
        review.id, observation.id, profile_id, version, values, row_number
    )


def locked_expected_review(
    session: Session,
    profile_id: UUID,
    review_item_id: UUID,
    observation_id: UUID,
    row_number: int,
) -> ExpectedReviewRow | None:
    """Lock and re-render one review row immediately before its transition."""
    result = session.execute(
        select(LabObservation, ReviewItem, Document)
        .join(ReviewItem, ReviewItem.observation_id == LabObservation.id)
        .join(Document, LabObservation.document_id == Document.id)
        .where(
            ReviewItem.id == review_item_id,
            LabObservation.id == observation_id,
            Document.profile_id == profile_id,
        )
        .with_for_update(of=(LabObservation, ReviewItem, Document))
    ).one_or_none()
    if result is None:
        return None
    observation, review, _document = result
    if observation.status != ReviewStatus.NEEDS_REVIEW:
        return None
    return _expected_review(session, observation, review, profile_id, row_number)


def build_projection(
    session: Session,
    profile_id: UUID,
    binding: WorkbookBinding,
    source_statuses: tuple[SourceStatusRow, ...] = (),
) -> ProjectionBundle:
    verified = session.execute(
        select(LabObservation, Document)
        .join(Document, LabObservation.document_id == Document.id)
        .where(
            Document.profile_id == profile_id,
            LabObservation.status == ReviewStatus.VERIFIED,
        )
        .order_by(
            func.coalesce(Document.collected_date, Document.issued_date),
            LabObservation.id,
        )
    ).all()
    labs = tuple(
        (
            _render_medical_date(document),
            observation.canonical_name,
            observation.source_name,
            _render(observation.normalized_value),
            observation.normalized_unit,
            observation.source_value,
            observation.source_unit,
            observation.reference_text,
            str(document.id),
            _source_link(session, document.id),
        )
        for observation, document in verified
    )

    review_rows = session.execute(
        select(LabObservation, ReviewItem, Document)
        .join(ReviewItem, ReviewItem.observation_id == LabObservation.id)
        .join(Document, LabObservation.document_id == Document.id)
        .where(Document.profile_id == profile_id)
        .order_by(ReviewItem.created_at, ReviewItem.id)
    ).all()
    audits = {
        audit.review_item_id: audit.row_version
        for audit in session.scalars(
            select(SheetsReviewDecisionAudit).where(
                SheetsReviewDecisionAudit.profile_id == profile_id
            )
        )
    }
    known = tuple(
        _expected_review(
            session,
            observation,
            review,
            profile_id,
            index + 2,
            audits.get(review.id),
        )
        for index, (observation, review, _document) in enumerate(review_rows)
    )
    pending = tuple(
        row
        for row, (observation, _review, _document) in zip(
            known, review_rows, strict=True
        )
        if observation.status == ReviewStatus.NEEDS_REVIEW
    )
    sorted_sources = tuple(
        status.values()
        for status in sorted(source_statuses, key=lambda row: (row.source, row.account))
    )
    workbook = WorkbookProjection(
        binding,
        (
            ManagedSheet("Lab history", LAB_HEADERS, labs),
            ManagedSheet(
                "Needs review",
                REVIEW_HEADERS,
                tuple(row.values() for row in pending),
                (12,),
            ),
            ManagedSheet("Sources", SOURCE_HEADERS, sorted_sources),
        ),
    )
    return ProjectionBundle(workbook, pending, known)
