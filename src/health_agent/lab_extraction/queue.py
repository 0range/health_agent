"""Short database transactions, serialized by a profile-wide worker lease."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from uuid import UUID, uuid4

from sqlalchemy import Engine, exists, func, select, text
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob, LabExtractionProfile
from health_agent.lab_extraction.registry import canonical_name, name_key, unit_key
from health_agent.lab_extraction.types import (
    EXTRACTOR_VERSION,
    Candidate,
    DocumentSnapshot,
    ExtractionError,
    declared_safe_code,
)
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewItem,
    ReviewStatus,
)


@contextmanager
def profile_lock(engine: Engine, profile_id: UUID) -> Iterator[None]:
    key = int.from_bytes(
        hashlib.sha256(b"lab-extraction-lock-v1" + profile_id.bytes).digest()[:8],
        "big",
        signed=True,
    )
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        if not connection.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
        ):
            raise ExtractionError("extraction_busy")
        try:
            yield
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


@dataclass(frozen=True, slots=True)
class Claim:
    id: UUID
    token: UUID
    document: DocumentSnapshot
    page_number: int
    text: str
    local_completed: bool


@dataclass(frozen=True, slots=True)
class QueueStatus:
    configured: bool
    enabled: bool = False
    cloud_enabled: bool = False
    queued: int = 0
    waiting_cloud: int = 0
    attention: int = 0
    completed: int = 0
    daily_budget: int = 0
    cloud_requests_today: int = 0


@dataclass(frozen=True, slots=True)
class JobDiagnostic:
    document_id: UUID
    page_number: int
    extractor_version: str
    state: str
    safe_error: str


def _locked_job(session: Session, claim: Claim) -> LabExtractionJob:
    job = session.scalar(
        select(LabExtractionJob)
        .where(
            LabExtractionJob.id == claim.id,
            LabExtractionJob.profile_id == claim.document.profile_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        job is None
        or job.claim_token != claim.token
        or job.status not in {"running", "cloud_in_flight"}
    ):
        raise ExtractionError("stale_extraction_claim")
    return job


def _finish(job: LabExtractionJob, status: str, code: str | None = None) -> None:
    job.status, job.safe_error_code, job.claim_token = status, code, None


def _value_key(name: str, value: str, unit: str | None) -> tuple[str, str, str]:
    # Keep qualifiers/signs and exact units. Historical rejected/superseded rows
    # participate too: a retry never resurrects an explicitly reviewed source row.
    return canonical_name(name), name_key(value).replace(",", "."), unit_key(unit or "")


class ExtractionQueue:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def configure(
        self, profile_id: UUID, *, enabled: bool, openai: bool, daily_budget: int
    ) -> None:
        if not 1 <= daily_budget <= 100:
            raise ExtractionError("invalid_daily_budget")
        with (
            profile_lock(self.engine, profile_id),
            session_scope(self.engine) as session,
        ):
            if session.get(Profile, profile_id) is None:
                raise ExtractionError("profile_not_found")
            config = session.get(LabExtractionProfile, profile_id)
            if config is None:
                config = LabExtractionProfile(profile_id=profile_id)
                session.add(config)
            config.enabled, config.cloud_enabled, config.daily_budget = (
                enabled,
                openai,
                daily_budget,
            )

    def status(self, profile_id: UUID, today: date) -> QueueStatus:
        with session_scope(self.engine) as session:
            config = session.get(LabExtractionProfile, profile_id)
            if config is None:
                return QueueStatus(False)
            counts = {
                status: count
                for status, count in session.execute(
                    select(LabExtractionJob.status, func.count())
                    .where(
                        LabExtractionJob.profile_id == profile_id,
                        LabExtractionJob.extractor_version == EXTRACTOR_VERSION,
                    )
                    .group_by(LabExtractionJob.status)
                ).all()
            }
            return QueueStatus(
                True,
                config.enabled,
                config.cloud_enabled,
                counts.get("queued", 0),
                counts.get("waiting_cloud", 0),
                counts.get("needs_attention", 0),
                counts.get("completed", 0),
                config.daily_budget,
                config.cloud_requests_today if config.cloud_day == today else 0,
            )

    def diagnostics(
        self, profile_id: UUID, *, limit: int = 20, offset: int = 0
    ) -> tuple[JobDiagnostic, ...]:
        if not 1 <= limit <= 100 or not 0 <= offset <= 1_000_000:
            raise ExtractionError("invalid_status_limit")
        with session_scope(self.engine) as session:
            rows = session.scalars(
                select(LabExtractionJob)
                .where(
                    LabExtractionJob.profile_id == profile_id,
                    LabExtractionJob.extractor_version == EXTRACTOR_VERSION,
                    LabExtractionJob.status != "completed",
                )
                .order_by(LabExtractionJob.document_id, LabExtractionJob.page_number)
                .limit(limit)
                .offset(offset)
            ).all()
            return tuple(
                JobDiagnostic(
                    row.document_id,
                    row.page_number,
                    EXTRACTOR_VERSION,
                    row.status,
                    declared_safe_code(row.safe_error_code)
                    if row.safe_error_code
                    else "none",
                )
                for row in rows
            )

    def discover_and_recover(self, profile_id: UUID) -> None:
        with session_scope(self.engine) as session:
            active = session.scalars(
                select(LabExtractionJob)
                .where(
                    LabExtractionJob.profile_id == profile_id,
                    LabExtractionJob.status.in_(("running", "cloud_in_flight")),
                )
                .with_for_update()
            ).all()
            for job in active:
                if job.status == "cloud_in_flight":
                    _finish(job, "needs_attention", "cloud_outcome_unknown")
                else:
                    _finish(job, "queued")
            pages = session.execute(
                select(Document.id, DocumentPage.page_number)
                .join(DocumentPage)
                .where(
                    Document.profile_id == profile_id,
                    Document.media_type.in_(
                        ("application/pdf", "image/jpeg", "image/png")
                    ),
                    ~exists(
                        select(LabExtractionJob.id).where(
                            LabExtractionJob.document_id == Document.id,
                            LabExtractionJob.page_number == DocumentPage.page_number,
                            LabExtractionJob.extractor_version == EXTRACTOR_VERSION,
                        )
                    ),
                )
                .order_by(Document.created_at, Document.id, DocumentPage.page_number)
                .limit(500)
            ).all()
            for document_id, page in pages:
                session.add(
                    LabExtractionJob(
                        profile_id=profile_id,
                        document_id=document_id,
                        page_number=page,
                        extractor_version=EXTRACTOR_VERSION,
                        status="queued" if page <= 100 else "needs_attention",
                        safe_error_code=None if page <= 100 else "page_limit",
                    )
                )

    def pending(self, profile_id: UUID, limit: int, *, cloud: bool) -> tuple[UUID, ...]:
        states = ("queued", "waiting_cloud") if cloud else ("queued",)
        with session_scope(self.engine) as session:
            return tuple(
                session.scalars(
                    select(LabExtractionJob.id)
                    .where(
                        LabExtractionJob.profile_id == profile_id,
                        LabExtractionJob.status.in_(states),
                        LabExtractionJob.extractor_version == EXTRACTOR_VERSION,
                    )
                    .order_by(LabExtractionJob.updated_at, LabExtractionJob.id)
                    .limit(limit)
                ).all()
            )

    def claim(self, profile_id: UUID, job_id: UUID) -> Claim:
        with session_scope(self.engine) as session:
            job = session.scalar(
                select(LabExtractionJob)
                .where(
                    LabExtractionJob.id == job_id,
                    LabExtractionJob.profile_id == profile_id,
                )
                .with_for_update()
            )
            if job is None or job.status not in {"queued", "waiting_cloud"}:
                raise ExtractionError("stale_extraction_claim")
            document = session.get_one(Document, job.document_id)
            page = session.scalars(
                select(DocumentPage).where(
                    DocumentPage.document_id == document.id,
                    DocumentPage.page_number == job.page_number,
                )
            ).one()
            if not page.extracted_text and session.scalar(
                select(
                    exists().where(
                        LabObservation.document_id == document.id,
                        LabObservation.page_number == page.page_number,
                    )
                )
            ):
                _finish(job, "needs_attention", "page_evidence_exists")
                session.flush()
                # Return a claim is unsafe: caller detects this before invoking OCR.
                return Claim(
                    job.id,
                    UUID(int=0),
                    DocumentSnapshot(
                        document.id,
                        profile_id,
                        document.sha256,
                        document.vault_path,
                        document.media_type,
                    ),
                    page.page_number,
                    "",
                    False,
                )
            job.status, job.claim_token = "running", uuid4()
            job.local_attempts += 1
            return Claim(
                job.id,
                job.claim_token,
                DocumentSnapshot(
                    document.id,
                    profile_id,
                    document.sha256,
                    document.vault_path,
                    document.media_type,
                ),
                page.page_number,
                page.extracted_text or "",
                job.local_completed,
            )

    def publish(
        self,
        claim: Claim,
        source_text: str,
        candidates: tuple[Candidate, ...],
        *,
        cloud: bool,
        unresolved: bool = False,
        cloud_method: str = "openai_structured",
    ) -> int:
        if cloud_method not in {"openai_structured", "yandex_structured"}:
            raise ExtractionError("cloud_request_rejected")
        with session_scope(self.engine) as session:
            # Same first lock as explicit review/date transitions. Never hold it
            # during OCR/OpenAI and never overwrite a reviewed observation.
            document = session.scalar(
                select(Document)
                .where(
                    Document.id == claim.document.id,
                    Document.profile_id == claim.document.profile_id,
                )
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if document is None:
                raise ExtractionError("document_not_found")
            page = session.scalars(
                select(DocumentPage)
                .where(
                    DocumentPage.document_id == document.id,
                    DocumentPage.page_number == claim.page_number,
                )
                .with_for_update()
            ).one()
            job = _locked_job(session, claim)
            if job.status != ("cloud_in_flight" if cloud else "running"):
                raise ExtractionError("stale_extraction_claim")
            if page.extracted_text and page.extracted_text != source_text:
                raise ExtractionError("page_evidence_changed")
            existing = session.scalars(
                select(LabObservation).where(
                    LabObservation.document_id == document.id,
                    LabObservation.page_number == claim.page_number,
                )
            ).all()
            if not page.extracted_text:
                if existing:
                    raise ExtractionError("page_evidence_exists")
                page.extracted_text, page.extraction_method = (
                    source_text,
                    "local_text_or_ocr",
                )
            digest = hashlib.sha256(source_text.encode()).hexdigest()
            if cloud and job.source_text_sha256 != digest:
                raise ExtractionError("page_evidence_changed")
            keys = {
                _value_key(row.source_name, row.source_value, row.source_unit)
                for row in existing
            }
            inserted = 0
            lifetime_candidates = (
                session.scalar(
                    select(
                        func.coalesce(func.sum(LabExtractionJob.candidate_count), 0)
                    ).where(
                        LabExtractionJob.document_id == document.id,
                        LabExtractionJob.page_number == claim.page_number,
                    )
                )
                or 0
            )
            for candidate in candidates:
                key = _value_key(
                    candidate.source_name, candidate.source_value, candidate.source_unit
                )
                if key in keys:
                    continue
                keys.add(key)
                if lifetime_candidates + inserted >= 40:
                    raise ExtractionError("candidate_limit")
                row = LabObservation(
                    document_id=document.id,
                    page_number=claim.page_number,
                    canonical_name=candidate.canonical_name,
                    source_name=candidate.source_name,
                    source_value=candidate.source_value,
                    source_unit=candidate.source_unit,
                    source_flag=candidate.source_flag,
                    parsed_value=candidate.parsed_value,
                    reference_low=candidate.reference_low,
                    reference_high=candidate.reference_high,
                    reference_text=candidate.reference_text,
                    evidence_excerpt=candidate.evidence_excerpt,
                    confidence=0.4
                    if candidate.canonical_name.startswith("unmapped_")
                    else (0.6 if cloud else 0.8),
                    status=ReviewStatus.NEEDS_REVIEW,
                )
                row.review_item = ReviewItem(
                    reason_code="lab_extraction_v1_cloud"
                    if cloud
                    else "lab_extraction_v1_local"
                )
                session.add(row)
                inserted += 1
            job.candidate_count += inserted
            job.source_text_sha256, job.extraction_method = (
                digest,
                cloud_method if cloud else "local_text",
            )
            job.local_completed = True
            if cloud or not unresolved:
                _finish(job, "completed")
            if inserted:
                if document.safe_error_code is None or document.safe_error_code in {
                    "no_lab_candidates",
                    "ocr_required",
                    "ocr_unavailable",
                }:
                    document.processing_status = "needs_review"
                if document.safe_error_code in {
                    "no_lab_candidates",
                    "ocr_required",
                    "ocr_unavailable",
                }:
                    session.flush()
                    empty_page = session.scalar(
                        select(
                            exists().where(
                                DocumentPage.document_id == document.id,
                                (DocumentPage.extracted_text.is_(None))
                                | (DocumentPage.extracted_text == ""),
                            )
                        )
                    )
                    if not empty_page:
                        document.safe_error_code = None
                    else:
                        document.processing_status = "needs_attention"
            return inserted

    def reserve_cloud(
        self, claim: Claim, today: date, model: str, *, allowed: bool
    ) -> bool:
        with session_scope(self.engine) as session:
            config = session.scalar(
                select(LabExtractionProfile)
                .where(LabExtractionProfile.profile_id == claim.document.profile_id)
                .with_for_update()
            )
            job = _locked_job(session, claim)
            page_jobs = session.scalars(
                select(LabExtractionJob)
                .where(
                    LabExtractionJob.document_id == job.document_id,
                    LabExtractionJob.page_number == job.page_number,
                )
                .with_for_update()
            ).all()
            if sum(page_job.cloud_attempts for page_job in page_jobs) >= 3:
                _finish(job, "needs_attention", "cloud_attempt_limit")
                return False
            if any(
                page_job.safe_error_code == "cloud_outcome_unknown"
                or (page_job.id != job.id and page_job.status == "cloud_in_flight")
                for page_job in page_jobs
            ):
                _finish(job, "needs_attention", "cloud_outcome_unknown")
                return False
            if config is None or not config.enabled:
                _finish(job, "waiting_cloud", "extraction_disabled")
                return False
            if config.cloud_day is None or today > config.cloud_day:
                config.cloud_day, config.cloud_requests_today = today, 0
            if (
                not allowed
                or not config.cloud_enabled
                or config.cloud_day != today
                or config.cloud_requests_today >= config.daily_budget
            ):
                _finish(job, "waiting_cloud", "cloud_budget_or_optin_required")
                return False
            config.cloud_requests_today += 1
            job.cloud_attempts += 1
            job.safe_error_code = None
            job.status, job.model_name = "cloud_in_flight", model
            return True

    def fail(self, claim: Claim, code: str) -> None:
        with session_scope(self.engine) as session:
            job = _locked_job(session, claim)
            _finish(job, "needs_attention", code)

    def retry(
        self, profile_id: UUID, document_id: UUID, *, acknowledge_unknown: bool
    ) -> int:
        with (
            profile_lock(self.engine, profile_id),
            session_scope(self.engine) as session,
        ):
            if (
                session.scalar(
                    select(Document.id).where(
                        Document.id == document_id, Document.profile_id == profile_id
                    )
                )
                is None
            ):
                raise ExtractionError("document_not_found")
            jobs = session.scalars(
                select(LabExtractionJob)
                .where(
                    LabExtractionJob.profile_id == profile_id,
                    LabExtractionJob.document_id == document_id,
                )
                .with_for_update()
            ).all()
            if (
                any(
                    job.status == "cloud_in_flight"
                    or job.safe_error_code == "cloud_outcome_unknown"
                    for job in jobs
                )
                and not acknowledge_unknown
            ):
                raise ExtractionError("unknown_retry_requires_acknowledgment")
            count = 0
            for job in jobs:
                if acknowledge_unknown and (
                    job.safe_error_code == "cloud_outcome_unknown"
                    or job.status == "cloud_in_flight"
                ):
                    _finish(job, "needs_attention", "cloud_unknown_acknowledged")
                if (
                    job.extractor_version == EXTRACTOR_VERSION
                    and job.status
                    in {"needs_attention", "waiting_cloud", "cloud_in_flight"}
                    and sum(
                        page_job.cloud_attempts
                        for page_job in jobs
                        if page_job.page_number == job.page_number
                    )
                    < 3
                ):
                    _finish(job, "queued")
                    count += 1
            return count
