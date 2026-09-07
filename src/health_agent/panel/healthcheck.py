"""Read-only, profile-scoped local data coverage for the management panel."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

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
from health_agent.pilot.storage import PilotRecord
from health_agent.whoop.models import WhoopCycle, WhoopSleep, WhoopWorkout

SessionScopeFactory = Callable[[], AbstractContextManager[Session]]
_MOSCOW = ZoneInfo("Europe/Moscow")


class HealthcheckReader:
    """Aggregate operational metadata without exposing clinical values."""

    def __init__(
        self,
        sessions: SessionScopeFactory,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._clock = clock

    def coverage(self, profile_id: UUID) -> DataCoverage:
        """Read independent sources in separate rollback-only transactions."""
        whoop = self._read(lambda session: self._latest_whoop(session, profile_id))
        labs = self._read(lambda session: self._lab_dates(session, profile_id))
        counts = self._read(lambda session: self._counts(session, profile_id))
        coros = self._read(lambda session: self._coros(session, profile_id))
        apple = self._read(lambda session: self._apple(session, profile_id))
        latest_whoop = None if whoop is _UNKNOWN else whoop
        whoop_status = (
            "unknown"
            if whoop is _UNKNOWN
            else ("empty" if whoop is None else "available")
        )
        if labs is _UNKNOWN:
            collected = issued = received = None
            labs_status = "unknown"
        else:
            collected, issued, received = labs
            labs_status = (
                "empty" if collected is issued is received is None else "available"
            )
        if counts is _UNKNOWN:
            queued = running = waiting = in_flight = needs_attention = None
            needs_review = verified = None
            pending = None
            extraction_status = "unknown"
        else:
            queue, needs_review, verified = counts
            queued, running, waiting, in_flight, needs_attention = queue
            pending = queued + running + waiting + in_flight
            extraction_status = (
                "empty" if sum(queue) == needs_review == verified == 0 else "available"
            )
        if coros is _UNKNOWN:
            coros_values = (None, None, None, None)
            coros_status = "unknown"
        else:
            coros_values = coros
            coros_status = "empty" if coros[0] == 0 else "available"
        if apple is _UNKNOWN:
            apple = (None, None, None, None, None, None, None)
            apple_status = "unknown"
        else:
            apple_status = "empty" if apple[0] == apple[4] == 0 else "available"
        status = (
            "empty"
            if whoop_status == labs_status == "empty"
            and extraction_status == "empty"
            and coros_status == apple_status == "empty"
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
            whoop_status=whoop_status,
            labs_status=labs_status,
            extraction_status=extraction_status,
            coros_status=coros_status,
            apple_status=apple_status,
            extraction_queued_count=queued,
            extraction_running_count=running,
            extraction_waiting_cloud_count=waiting,
            extraction_cloud_in_flight_count=in_flight,
            extraction_needs_attention_count=needs_attention,
            coros_activity_count=coros_values[0],
            coros_first_date=coros_values[1],
            coros_latest_date=coros_values[2],
            coros_last_sync_at=coros_values[3],
            apple_weight_count=apple[0],
            apple_weight_first_date=apple[1],
            apple_weight_latest_date=apple[2],
            apple_imported_at=apple[3],
            apple_workout_candidate_count=apple[4],
            apple_workout_first_date=apple[5],
            apple_workout_latest_date=apple[6],
        )

    def _read(self, operation):  # type: ignore[no-untyped-def]
        try:
            with self._sessions() as session:
                result = operation(session)
                session.rollback()
                return result
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
            select(
                func.max(Document.collected_date), func.max(Document.issued_date)
            ).where(
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
    def _counts(session: Session, profile_id: UUID) -> tuple[tuple[int, ...], int, int]:
        job_rows = {
            str(status): int(count)
            for status, count in session.execute(
                select(LabExtractionJob.status, func.count(LabExtractionJob.id))
                .where(LabExtractionJob.profile_id == profile_id)
                .group_by(LabExtractionJob.status)
            )
        }
        states = (
            "queued",
            "running",
            "waiting_cloud",
            "cloud_in_flight",
            "needs_attention",
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
            tuple(int(job_rows.get(state, 0)) for state in states),
            int(statuses.get(ReviewStatus.NEEDS_REVIEW, 0)),
            int(statuses.get(ReviewStatus.VERIFIED, 0)),
        )

    def _coros(self, session: Session, profile_id: UUID):  # type: ignore[no-untyped-def]
        rows = tuple(
            session.execute(
                select(PilotRecord.kind, PilotRecord.payload).where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.domain == "training",
                    PilotRecord.kind.in_(("activity", "sync_run")),
                )
            )
        )
        now = self._clock().astimezone(UTC)
        coros_dates = [
            _source_date(payload, ("started_at", "date"), now)
            for kind, payload in rows
            if kind == "activity"
        ]
        syncs = [
            _source_datetime(payload, ("finished_at",), now)
            for kind, payload in rows
            if kind == "sync_run" and payload.get("status") in {"success", "incomplete"}
        ]
        return _range(coros_dates, syncs)

    def _apple(self, session: Session, profile_id: UUID):  # type: ignore[no-untyped-def]
        rows = tuple(
            session.execute(
                select(
                    PilotRecord.domain,
                    PilotRecord.kind,
                    PilotRecord.at,
                    PilotRecord.payload,
                ).where(
                    PilotRecord.profile_id == profile_id,
                    or_(
                        (PilotRecord.domain == "shared")
                        & PilotRecord.kind.in_(("weight", "apple_import")),
                        (PilotRecord.domain == "training")
                        & (PilotRecord.kind == "apple_workout"),
                    ),
                )
            )
        )
        now = self._clock().astimezone(UTC)
        weights = [
            _source_date(payload, ("recorded_at",), now)
            for domain, kind, _at, payload in rows
            if domain == "shared"
            and kind == "weight"
            and payload.get("source") == "apple_health"
        ]
        workouts = [
            _source_date(payload, ("started_at",), now)
            for domain, kind, _at, payload in rows
            if domain == "training"
            and kind == "apple_workout"
            and payload.get("source") == "apple_health"
        ]
        imports = [
            at.astimezone(UTC)
            for domain, kind, at, _payload in rows
            if domain == "shared"
            and kind == "apple_import"
            and at.tzinfo is not None
            and at.astimezone(UTC) <= now
        ]
        return (
            *_date_range(weights),
            max(imports, default=None),
            *_date_range(workouts),
        )


_UNKNOWN = object()


def _source_datetime(
    payload: dict[str, object], keys: tuple[str, ...], now: datetime
) -> datetime | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            continue
        if parsed.tzinfo is not None and parsed.astimezone(UTC) <= now:
            return parsed.astimezone(UTC)
    return None


def _source_date(
    payload: dict[str, object], keys: tuple[str, ...], now: datetime
) -> date | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed_date = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return (
                parsed_date if parsed_date <= now.astimezone(_MOSCOW).date() else None
            )
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.astimezone(UTC) > now:
                continue
            parsed_date = parsed.astimezone(_MOSCOW).date()
        except ValueError:
            continue
        return parsed_date
    return None


def _date_range(values: list[date | None]) -> tuple[int, date | None, date | None]:
    valid = [value for value in values if value is not None]
    return len(valid), min(valid, default=None), max(valid, default=None)


def _range(
    values: list[date | None], syncs: list[datetime | None]
) -> tuple[int, date | None, date | None, datetime | None]:
    count, first, latest = _date_range(values)
    return (
        count,
        first,
        latest,
        max((value for value in syncs if value is not None), default=None),
    )
