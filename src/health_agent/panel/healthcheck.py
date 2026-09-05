"""Read-only, profile-scoped local data coverage for the management panel."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from health_agent.lab_extraction.models import LabExtractionJob
from health_agent.models import (
    Document,
    DocumentSourceRecord,
    LabObservation,
    ReviewStatus,
    SourceRecord,
)
from health_agent.panel.models import DataCoverage
from health_agent.whoop.models import WhoopCycle, WhoopSleep, WhoopWorkout

SessionScopeFactory = Callable[[], AbstractContextManager[Session]]


class HealthcheckReader:
    """Aggregate operational metadata without exposing clinical values."""

    def __init__(self, sessions: SessionScopeFactory) -> None:
        self._sessions = sessions

    def coverage(self, profile_id: UUID) -> DataCoverage:
        """Read every query in a rollback-only transaction scoped by profile."""
        with self._sessions() as session:
            whoop = self._safe(lambda: self._latest_whoop(session, profile_id))
            labs = self._safe(lambda: self._lab_dates(session, profile_id))
            counts = self._safe(lambda: self._counts(session, profile_id))
            session.rollback()
        if whoop is _UNKNOWN or labs is _UNKNOWN or counts is _UNKNOWN:
            return DataCoverage(status="unknown")
        latest_whoop = whoop
        collected, issued, received = labs
        pending, needs_review, verified = counts
        status = (
            "empty"
            if latest_whoop is None
            and collected is None
            and issued is None
            and received is None
            and pending == needs_review == verified == 0
            else "available"
        )
        return DataCoverage(
            status=status,
            latest_whoop_date=latest_whoop,
            latest_lab_collected_date=collected,
            latest_lab_issued_date=issued,
            latest_received_at=received,
            pending_extraction_count=pending,
            needs_review_count=needs_review,
            verified_count=verified,
        )

    @staticmethod
    def _safe(operation):  # type: ignore[no-untyped-def]
        try:
            return operation()
        except Exception:  # noqa: BLE001 - raw local errors must not reach the page.
            return _UNKNOWN

    @staticmethod
    def _latest_whoop(session: Session, profile_id: UUID) -> date | None:
        dates = (
            session.scalar(
                select(func.max(model.local_day)).where(model.profile_id == profile_id)
            )
            for model in (WhoopCycle, WhoopSleep, WhoopWorkout)
        )
        return max((value for value in dates if value is not None), default=None)

    @staticmethod
    def _lab_dates(
        session: Session, profile_id: UUID
    ) -> tuple[date | None, date | None, datetime | None]:
        collected, issued = session.execute(
            select(func.max(Document.collected_date), func.max(Document.issued_date)).where(
                Document.profile_id == profile_id,
                or_(
                    Document.document_type == "laboratory_report",
                    Document.observations.any(),
                ),
            )
        ).one()
        received = session.scalar(
            select(func.max(SourceRecord.received_at))
            .join(
                DocumentSourceRecord,
                DocumentSourceRecord.source_record_id == SourceRecord.id,
            )
            .where(
                SourceRecord.profile_id == profile_id,
                DocumentSourceRecord.profile_id == profile_id,
            )
        )
        return collected, issued, received

    @staticmethod
    def _counts(session: Session, profile_id: UUID) -> tuple[int, int, int]:
        pending = session.scalar(
            select(func.count(LabExtractionJob.id)).where(
                LabExtractionJob.profile_id == profile_id,
                LabExtractionJob.status.in_(
                    ("queued", "running", "waiting_cloud", "cloud_in_flight")
                ),
            )
        )
        status_rows = session.execute(
            select(LabObservation.status, func.count(LabObservation.id))
            .join(Document, Document.id == LabObservation.document_id)
            .where(Document.profile_id == profile_id)
            .group_by(LabObservation.status)
        )
        statuses: dict[ReviewStatus, int] = {
            status: int(count) for status, count in status_rows
        }
        return (
            int(pending or 0),
            int(statuses.get(ReviewStatus.NEEDS_REVIEW, 0)),
            int(statuses.get(ReviewStatus.VERIFIED, 0)),
        )


_UNKNOWN = object()
