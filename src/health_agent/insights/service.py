"""Read-only deterministic projection of verified labs and WHOOP observations."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import isfinite
from statistics import fmean
from uuid import UUID

from sqlalchemy import String, and_, cast, func, or_, select
from sqlalchemy.orm import Session

from health_agent.insights.models import (
    HealthSignal,
    HealthSnapshot,
    SignalKind,
    SignalState,
    SourceCitation,
)
from health_agent.models import Document, LabObservation, ReviewStatus
from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopCycle,
    WhoopRecovery,
    WhoopSleep,
)

MAX_LAB_ROWS = 200
MAX_PRIORITY_SIGNALS = 5
RECENT_DAYS = 7
BASELINE_DAYS = 28
MIN_RECENT_DAYS = 4
MIN_BASELINE_DAYS = 14


class HealthSnapshotBuilder:
    """Build a bounded profile-scoped snapshot without mutating persistence."""

    def __init__(
        self, session: Session, clock: Callable[[], datetime] | None = None
    ) -> None:
        self._session = session
        self._clock = clock or (lambda: datetime.now(UTC))

    def build(self, profile_id: UUID) -> HealthSnapshot:
        as_of = _utc(self._clock())
        signals = [*self._labs(profile_id, as_of), *self._wearables(profile_id, as_of)]
        weight = self._weight(profile_id, as_of)
        if weight is not None:
            signals.append(weight)
        ordered = tuple(
            sorted(
                signals, key=lambda item: (item.observed_at, item.title), reverse=True
            )
        )
        return HealthSnapshot(
            profile_id=profile_id,
            as_of=as_of,
            attention=tuple(
                item for item in ordered if item.state is SignalState.ATTENTION
            )[:MAX_PRIORITY_SIGNALS],
            stable=tuple(item for item in ordered if item.state is SignalState.STABLE)[
                :MAX_PRIORITY_SIGNALS
            ],
            gaps=tuple(item for item in ordered if item.state is SignalState.GAP)[
                :MAX_PRIORITY_SIGNALS
            ],
            signals=ordered,
        )

    def _labs(self, profile_id: UUID, as_of: datetime) -> list[HealthSignal]:
        observed_on = func.coalesce(Document.collected_date, Document.issued_date)
        rows = self._session.execute(
            select(LabObservation, Document, observed_on)
            .join(Document, LabObservation.document_id == Document.id)
            .where(
                Document.profile_id == profile_id,
                LabObservation.status == ReviewStatus.VERIFIED,
                or_(observed_on <= as_of.date(), observed_on.is_(None)),
            )
            .order_by(
                observed_on.desc().nullslast(),
                LabObservation.created_at.desc(),
                LabObservation.id.desc(),
            )
            .limit(MAX_LAB_ROWS)
        )
        result: list[HealthSignal] = []
        seen: set[tuple[str, str | None]] = set()
        for observation, document, day in rows:
            key = (observation.canonical_name.casefold(), observation.source_unit)
            if key in seen:
                continue
            seen.add(key)
            citation = SourceCitation(
                citation_id=f"[SNAP{len(result) + 1}]",
                source_kind="lab",
                source_id=str(observation.id),
                observed_on=day,
                page_number=observation.page_number,
            )
            observed_at = (
                datetime.combine(day, datetime.min.time(), UTC) if day else as_of
            )
            value = observation.parsed_value
            low, high = observation.reference_low, observation.reference_high
            state = SignalState.GAP
            summary = "Недостаточно данных для сопоставления с диапазоном лаборатории"
            if day is None:
                summary = "Дата анализа не указана; результат нельзя уверенно разместить во времени"
            elif not _finite(value) or not _valid_bounds(low, high):
                summary = "Значение или границы лаборатории непригодны для безопасного сопоставления"
            elif low is None and high is None:
                summary = "В источнике не указан референсный диапазон"
            else:
                outside = (low is not None and value < low) or (
                    high is not None and value > high
                )
                state = SignalState.ATTENTION if outside else SignalState.STABLE
                summary = (
                    "Вне указанного лабораторией референсного диапазона"
                    if outside
                    else "В пределах указанного лабораторией референсного диапазона"
                )
            result.append(
                HealthSignal(
                    kind=SignalKind.LAB,
                    state=state,
                    title=observation.canonical_name,
                    summary=summary,
                    observed_at=observed_at,
                    citations=(citation,),
                    value=observation.source_value,
                    unit=observation.source_unit,
                    reference=_reference(observation),
                    explanation_key=observation.canonical_name.casefold(),
                )
            )
        return result

    def _wearables(self, profile_id: UUID, as_of: datetime) -> list[HealthSignal]:
        start = as_of - timedelta(days=RECENT_DAYS + BASELINE_DAYS)
        sleeps = self._session.scalars(
            select(WhoopSleep).where(
                WhoopSleep.profile_id == profile_id,
                WhoopSleep.start_at >= start,
                WhoopSleep.start_at <= as_of,
                WhoopSleep.is_nap.is_not(True),
            )
        ).all()
        sleep_values = [
            (row.start_at, float(row.total_sleep_milli) / 3_600_000)
            for row in sleeps
            if row.total_sleep_milli is not None
            and 0 < row.total_sleep_milli <= 86_400_000
            and row.score_state not in {"PENDING_SCORE", "UNSCORABLE"}
        ]
        signals = [
            self._trend(
                "Продолжительность сна", "ч", sleep_values, as_of, "whoop_sleep"
            )
        ]

        physiological_at = func.coalesce(WhoopSleep.start_at, WhoopCycle.start_at)
        recoveries = self._session.execute(
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
                physiological_at >= start,
                physiological_at <= as_of,
            )
        )
        recovery_values = [
            (when, float(row.recovery_score))
            for row, when in recoveries
            if when is not None
            and row.recovery_score is not None
            and 0 <= row.recovery_score <= 100
            and row.score_state not in {"PENDING_SCORE", "UNSCORABLE"}
        ]
        signals.append(
            self._trend(
                "Показатель восстановления",
                "%",
                recovery_values,
                as_of,
                "whoop_recovery",
            )
        )
        return signals

    def _trend(
        self,
        title: str,
        unit: str,
        rows: Iterable[tuple[datetime, float]],
        as_of: datetime,
        source: str,
    ) -> HealthSignal:
        daily: dict[date, list[float]] = defaultdict(list)
        for when, value in rows:
            when_utc = _utc(when)
            if when_utc <= as_of and isfinite(value):
                daily[when_utc.date()].append(value)
        recent_start = (as_of - timedelta(days=RECENT_DAYS)).date()
        baseline_start = (as_of - timedelta(days=RECENT_DAYS + BASELINE_DAYS)).date()
        recent = [
            fmean(v) for d, v in daily.items() if recent_start < d <= as_of.date()
        ]
        baseline = [
            fmean(v) for d, v in daily.items() if baseline_start < d <= recent_start
        ]
        citation = SourceCitation(
            f"[SNAP-{source.upper()}]", source, source, as_of.date()
        )
        if len(recent) < MIN_RECENT_DAYS or len(baseline) < MIN_BASELINE_DAYS:
            return HealthSignal(
                SignalKind.WEARABLE,
                SignalState.GAP,
                title,
                f"Недостаточно полных дней: последние 7 — {len(recent)}, предыдущие 28 — {len(baseline)}",
                as_of,
                (citation,),
            )
        current, previous = fmean(recent), fmean(baseline)
        relative = (current - previous) / previous * 100 if previous else None
        direction = (
            "выше"
            if current > previous
            else "ниже"
            if current < previous
            else "без изменения"
        )
        detail = f"Среднее за 7 дней {direction} среднего за предыдущие 28 дней"
        if relative is not None:
            detail += f" на {abs(relative):.1f}%"
        return HealthSignal(
            SignalKind.WEARABLE,
            SignalState.OBSERVED,
            title,
            detail,
            as_of,
            (citation,),
            value=f"{current:.1f}",
            unit=unit,
            explanation_key=source,
        )

    def _weight(self, profile_id: UUID, as_of: datetime) -> HealthSignal | None:
        row = self._session.scalars(
            select(WhoopBodyCurrent)
            .where(
                WhoopBodyCurrent.profile_id == profile_id,
                WhoopBodyCurrent.observed_at <= as_of,
                WhoopBodyCurrent.weight_kilogram.is_not(None),
            )
            .order_by(WhoopBodyCurrent.observed_at.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        assert row.weight_kilogram is not None
        citation = SourceCitation(
            "[SNAP-WEIGHT]", "whoop_weight", str(row.id), row.observed_at.date()
        )
        return HealthSignal(
            SignalKind.WEIGHT,
            SignalState.OBSERVED,
            "Текущий вес WHOOP",
            "Снимок на момент синхронизации; это не динамика веса",
            _utc(row.observed_at),
            (citation,),
            value=_number(row.weight_kilogram),
            unit="кг",
        )


def _finite(value: Decimal | None) -> bool:
    return value is not None and value.is_finite()


def _valid_bounds(low: Decimal | None, high: Decimal | None) -> bool:
    return (
        (low is None or low.is_finite())
        and (high is None or high.is_finite())
        and not (low is not None and high is not None and low > high)
    )


def _reference(item: LabObservation) -> str | None:
    if item.reference_text:
        return item.reference_text
    if item.reference_low is not None and item.reference_high is not None:
        return f"{_number(item.reference_low)}–{_number(item.reference_high)}"
    if item.reference_low is not None:
        return f"≥ {_number(item.reference_low)}"
    if item.reference_high is not None:
        return f"≤ {_number(item.reference_high)}"
    return None


def _number(value: Decimal) -> str:
    return format(value.normalize(), "f")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
