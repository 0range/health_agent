"""Read-only SQLAlchemy retrieval for bounded, profile-scoped question evidence."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from sqlalchemy import Select, String, and_, cast, func, select
from sqlalchemy.orm import Session

from health_agent.models import Document, LabObservation, ReviewStatus
from health_agent.questions.models import (
    ContextLimitation,
    ContextLimitationCode,
    EvidenceItem,
    EvidenceSource,
    EvidenceTimeSemantics,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopCycle,
    WhoopRecovery,
    WhoopSleep,
    WhoopWorkout,
)

DEFAULT_WINDOW_DAYS = 30
SLEEP_RECOVERY_WINDOW_DAYS = 14
WEIGHT_TREND_WINDOW_DAYS = 90
MAX_ITEMS_PER_SOURCE = 10

_SOURCE_ORDER = (
    EvidenceSource.LAB,
    EvidenceSource.SLEEP,
    EvidenceSource.RECOVERY,
    EvidenceSource.CYCLE,
    EvidenceSource.WORKOUT,
    EvidenceSource.WEIGHT,
)
_SLEEP_RECOVERY_TERMS = (
    "sleep",
    "asleep",
    "insomnia",
    "recovery",
    "resting heart",
    "hrv",
    "сон",
    "сплю",
    "бессон",
    "восстанов",
    "пульс в покое",
)
_WEIGHT_TREND_TERMS = (
    "weight",
    "weigh",
    "bmi",
    "trend",
    "change over time",
    "вес",
    "динамик",
    "тренд",
)

type EvidenceRows = tuple[EvidenceItem, ...]


class HealthContextBuilder:
    """Builds safe evidence without mutating the database or exposing raw records."""

    def __init__(
        self,
        session: Session,
        *,
        clock: Callable[[], datetime] | None = None,
        max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
    ) -> None:
        if max_items_per_source < 1:
            raise ValueError("max_items_per_source must be positive")
        self._session = session
        self._clock = clock or _utc_now
        self._max_items_per_source = max_items_per_source

    def build(self, profile_id: UUID, question: str) -> HealthQuestionContext:
        """Return only display-ready facts that belong to ``profile_id``."""

        intent = detect_intent(question)
        window_end = _as_utc(self._clock())
        window_start = window_end - timedelta(days=window_days(intent))
        rows_by_source = {
            EvidenceSource.LAB: self._labs(profile_id, window_start, window_end),
            EvidenceSource.SLEEP: self._sleeps(profile_id, window_start, window_end),
            EvidenceSource.RECOVERY: self._recoveries(
                profile_id, window_start, window_end
            ),
            EvidenceSource.CYCLE: self._cycles(profile_id, window_start, window_end),
            EvidenceSource.WORKOUT: self._workouts(
                profile_id, window_start, window_end
            ),
            EvidenceSource.WEIGHT: self._weights(profile_id, window_start, window_end),
        }
        evidence = _label_evidence(rows_by_source)
        return HealthQuestionContext(
            profile_id=profile_id,
            intent=intent,
            window_start=window_start,
            window_end=window_end,
            evidence=evidence,
            source_counts={source: len(rows_by_source[source]) for source in _SOURCE_ORDER},
            limitations=_limitations_for(intent),
        )

    def _labs(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        observed_on = func.coalesce(Document.collected_date, Document.issued_date)
        statement = (
            select(LabObservation, observed_on)
            .join(Document, LabObservation.document_id == Document.id)
            .where(
                Document.profile_id == profile_id,
                LabObservation.status == ReviewStatus.VERIFIED,
                observed_on >= window_start.date(),
                observed_on <= window_end.date(),
            )
            .order_by(observed_on.desc(), LabObservation.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                citation_label="",
                source=EvidenceSource.LAB,
                observed_at=datetime.combine(observed_at, datetime.min.time(), UTC),
                metric=observation.canonical_name,
                value=_display_number(observation.normalized_value),
                unit=observation.normalized_unit,
            )
            for observation, observed_at in self._session.execute(statement)
            if observed_at is not None and observation.normalized_value is not None
        )

    def _sleeps(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        statement: Select[tuple[WhoopSleep]] = (
            select(WhoopSleep)
            .where(
                WhoopSleep.profile_id == profile_id,
                WhoopSleep.start_at >= window_start,
                WhoopSleep.start_at <= window_end,
            )
            .order_by(WhoopSleep.start_at.desc(), WhoopSleep.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                "",
                EvidenceSource.SLEEP,
                _as_utc(record.start_at),
                "Sleep duration",
                _display_hours(record.total_sleep_milli),
                "hours",
            )
            for record in self._session.scalars(statement)
            if record.total_sleep_milli is not None
        )

    def _recoveries(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        """Date recoveries by their associated sleep, falling back to the cycle.

        ``sleep_id`` and ``cycle_id`` are used only inside this normalized,
        profile-and-connection-scoped join; no external identifier reaches evidence.
        """

        physiological_at = func.coalesce(WhoopSleep.start_at, WhoopCycle.start_at)
        statement = (
            select(WhoopRecovery, physiological_at)
            .outerjoin(
                WhoopSleep,
                and_(
                    WhoopSleep.profile_id == WhoopRecovery.profile_id,
                    WhoopSleep.connection_id == WhoopRecovery.connection_id,
                    WhoopSleep.external_id == WhoopRecovery.sleep_id,
                ),
            )
            .outerjoin(
                WhoopCycle,
                and_(
                    WhoopCycle.profile_id == WhoopRecovery.profile_id,
                    WhoopCycle.connection_id == WhoopRecovery.connection_id,
                    WhoopCycle.external_id == cast(WhoopRecovery.cycle_id, String),
                ),
            )
            .where(
                WhoopRecovery.profile_id == profile_id,
                physiological_at >= window_start,
                physiological_at <= window_end,
            )
            .order_by(physiological_at.desc(), WhoopRecovery.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                "",
                EvidenceSource.RECOVERY,
                _as_utc(observed_at),
                "Recovery score",
                _display_number(record.recovery_score),
                "%",
            )
            for record, observed_at in self._session.execute(statement)
            if observed_at is not None and record.recovery_score is not None
        )

    def _cycles(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        statement: Select[tuple[WhoopCycle]] = (
            select(WhoopCycle)
            .where(
                WhoopCycle.profile_id == profile_id,
                WhoopCycle.start_at >= window_start,
                WhoopCycle.start_at <= window_end,
            )
            .order_by(WhoopCycle.start_at.desc(), WhoopCycle.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                "",
                EvidenceSource.CYCLE,
                _as_utc(record.start_at),
                "Cycle strain",
                _display_number(record.strain),
                None,
            )
            for record in self._session.scalars(statement)
            if record.strain is not None
        )

    def _workouts(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        statement: Select[tuple[WhoopWorkout]] = (
            select(WhoopWorkout)
            .where(
                WhoopWorkout.profile_id == profile_id,
                WhoopWorkout.start_at >= window_start,
                WhoopWorkout.start_at <= window_end,
            )
            .order_by(WhoopWorkout.start_at.desc(), WhoopWorkout.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                "",
                EvidenceSource.WORKOUT,
                _as_utc(record.start_at),
                "Workout strain",
                _display_number(record.strain),
                None,
            )
            for record in self._session.scalars(statement)
            if record.strain is not None
        )

    def _weights(
        self, profile_id: UUID, window_start: datetime, window_end: datetime
    ) -> EvidenceRows:
        """Return only a current WHOOP body snapshot, never a dated measurement."""

        statement: Select[tuple[WhoopBodyCurrent]] = (
            select(WhoopBodyCurrent)
            .where(
                WhoopBodyCurrent.profile_id == profile_id,
                WhoopBodyCurrent.weight_kilogram.is_not(None),
            )
            .order_by(WhoopBodyCurrent.observed_at.desc(), WhoopBodyCurrent.id.desc())
            .limit(self._max_items_per_source)
        )
        return tuple(
            EvidenceItem(
                "",
                EvidenceSource.WEIGHT,
                _as_utc(record.sync_as_of),
                "WHOOP current weight (synced as of)",
                _display_number(record.weight_kilogram),
                "kg",
                EvidenceTimeSemantics.SYNC_AS_OF,
            )
            for record in self._session.scalars(statement)
            if record.weight_kilogram is not None
        )


def build_context(
    session: Session,
    profile_id: UUID,
    question: str,
    *,
    clock: Callable[[], datetime] | None = None,
    max_items_per_source: int = MAX_ITEMS_PER_SOURCE,
) -> HealthQuestionContext:
    """Convenience entry point for one transaction-bound context build."""

    return HealthContextBuilder(
        session, clock=clock, max_items_per_source=max_items_per_source
    ).build(profile_id, question)


def detect_intent(question: str) -> QuestionIntent:
    normalized = question.casefold()
    if any(term in normalized for term in _SLEEP_RECOVERY_TERMS):
        return QuestionIntent.SLEEP_RECOVERY
    if any(term in normalized for term in _WEIGHT_TREND_TERMS):
        return QuestionIntent.WEIGHT_TREND
    return QuestionIntent.GENERAL


def window_days(intent: QuestionIntent) -> int:
    return {
        QuestionIntent.GENERAL: DEFAULT_WINDOW_DAYS,
        QuestionIntent.SLEEP_RECOVERY: SLEEP_RECOVERY_WINDOW_DAYS,
        QuestionIntent.WEIGHT_TREND: WEIGHT_TREND_WINDOW_DAYS,
    }[intent]


def _label_evidence(
    rows_by_source: dict[EvidenceSource, EvidenceRows],
) -> tuple[EvidenceItem, ...]:
    labelled: list[EvidenceItem] = []
    for source in _SOURCE_ORDER:
        for number, item in enumerate(rows_by_source[source], start=1):
            labelled.append(
                EvidenceItem(
                    citation_label=f"[{source.citation_prefix}{number}]",
                    source=source,
                    observed_at=item.observed_at,
                    metric=item.metric,
                    value=item.value,
                    unit=item.unit,
                    time_semantics=item.time_semantics,
                )
            )
    return tuple(labelled)


def _limitations_for(intent: QuestionIntent) -> tuple[ContextLimitation, ...]:
    if intent is not QuestionIntent.WEIGHT_TREND:
        return ()
    return (
        ContextLimitation(
            ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY,
            "WHOOP provides a current body snapshot synced as of its timestamp, not "
            "dated measurement history. Fewer than two dated measurements are "
            "available, so a weight change cannot be established.",
            prevents_requested_inference=True,
        ),
    )


def _display_hours(milliseconds: int | None) -> str:
    assert milliseconds is not None
    return _display_number(Decimal(milliseconds) / Decimal(3_600_000))


def _display_number(value: Decimal | int | None) -> str:
    assert value is not None
    if isinstance(value, Decimal):
        rendered = format(value.normalize(), "f")
        if "." in rendered:
            return rendered.rstrip("0").rstrip(".") or "0"
        return rendered
    return str(value)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)
