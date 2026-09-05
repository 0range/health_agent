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
from sqlalchemy.orm import Session, aliased

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
MAX_WEARABLE_ROWS = 500
MAX_SIGNAL_CITATIONS = 35
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
        labs = self._labs(profile_id, as_of)
        signals = [*labs, *self._lab_quality_gaps(profile_id, as_of, bool(labs))]
        signals.extend(self._wearables(profile_id, as_of))
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
            gaps=tuple(
                sorted(
                    (item for item in ordered if item.state is SignalState.GAP),
                    key=lambda item: item.kind is SignalKind.LAB,
                    reverse=True,
                )[:MAX_PRIORITY_SIGNALS]
            ),
            signals=ordered,
        )

    def _labs(self, profile_id: UUID, as_of: datetime) -> list[HealthSignal]:
        observed_on = func.coalesce(Document.collected_date, Document.issued_date)
        rank = func.row_number().over(
            partition_by=(
                func.lower(LabObservation.canonical_name),
                LabObservation.source_unit,
            ),
            order_by=(
                observed_on.desc().nullslast(),
                LabObservation.created_at.desc(),
                LabObservation.id.desc(),
            ),
        )
        ranked = (
            select(
                LabObservation.id.label("observation_id"),
                observed_on.label("observed_on"),
                rank.label("position"),
            )
            .join(Document, LabObservation.document_id == Document.id)
            .where(
                Document.profile_id == profile_id,
                LabObservation.status == ReviewStatus.VERIFIED,
                or_(observed_on <= as_of.date(), observed_on.is_(None)),
            )
            .subquery()
        )
        observation = aliased(LabObservation)
        rows = self._session.execute(
            select(observation, ranked.c.observed_on)
            .join(ranked, observation.id == ranked.c.observation_id)
            .where(ranked.c.position == 1)
            .order_by(ranked.c.observed_on.desc().nullslast(), observation.id.desc())
            .limit(MAX_LAB_ROWS)
        )
        result: list[HealthSignal] = []
        for observation, day in rows:
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
            elif _has_qualifier(observation.source_value):
                summary = "Результат содержит квалификатор; точное сопоставление с диапазоном неизвестно"
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
                    explanation_key=_explanation_key(observation.canonical_name),
                )
            )
        return result

    def _lab_quality_gaps(
        self, profile_id: UUID, as_of: datetime, has_verified: bool
    ) -> list[HealthSignal]:
        counts: dict[ReviewStatus, int] = {
            status: count
            for status, count in self._session.execute(
                select(LabObservation.status, func.count(LabObservation.id))
                .join(Document, LabObservation.document_id == Document.id)
                .where(Document.profile_id == profile_id)
                .group_by(LabObservation.status)
            )
        }
        pending = counts.get(ReviewStatus.NEEDS_REVIEW, 0)
        total = sum(counts.values())
        if pending:
            summary = f"{pending} лабораторных результатов ожидают проверки; их значения не показаны"
        elif not total and not has_verified:
            summary = "Проверенных лабораторных результатов пока нет"
        else:
            return []
        return [
            HealthSignal(
                SignalKind.LAB,
                SignalState.GAP,
                "Качество лабораторных данных",
                summary,
                as_of,
                (
                    SourceCitation(
                        "[SNAP-LAB-QUALITY]", "lab_status", "aggregate", as_of.date()
                    ),
                ),
            )
        ]

    def _wearables(self, profile_id: UUID, as_of: datetime) -> list[HealthSignal]:
        today = datetime.combine(as_of.date(), datetime.min.time(), UTC)
        start = today - timedelta(days=RECENT_DAYS + BASELINE_DAYS)
        sleeps = self._session.scalars(
            select(WhoopSleep)
            .where(
                WhoopSleep.profile_id == profile_id,
                WhoopSleep.start_at >= start,
                WhoopSleep.start_at < today,
                WhoopSleep.is_nap.is_not(True),
            )
            .limit(MAX_WEARABLE_ROWS)
        ).all()
        sleep_values = [
            (row.start_at, float(row.total_sleep_milli) / 3_600_000, str(row.id))
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
        sleep_performance = [
            (row.start_at, float(row.sleep_performance_percentage), str(row.id))
            for row in sleeps
            if row.sleep_performance_percentage is not None
            and 0 <= row.sleep_performance_percentage <= 100
            and row.score_state not in {"PENDING_SCORE", "UNSCORABLE"}
        ]
        signals.append(
            self._trend(
                "Эффективность сна WHOOP",
                "%",
                sleep_performance,
                as_of,
                "whoop_sleep_performance",
            )
        )

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
                physiological_at < today,
            )
            .limit(MAX_WEARABLE_ROWS)
        )
        recovery_rows = list(recoveries)
        for title, unit, attribute, source, valid in (
            (
                "Показатель восстановления",
                "%",
                "recovery_score",
                "whoop_recovery",
                lambda v: 0 <= v <= 100,
            ),
            (
                "Вариабельность сердечного ритма",
                "мс",
                "hrv_rmssd_milli",
                "whoop_hrv",
                lambda v: v > 0,
            ),
            (
                "Пульс в покое",
                "уд/мин",
                "resting_heart_rate",
                "whoop_rhr",
                lambda v: v > 0,
            ),
        ):
            values = [
                (when, float(value), str(row.id))
                for row, when in recovery_rows
                if when is not None
                and (value := getattr(row, attribute)) is not None
                and valid(value)
                and row.score_state not in {"PENDING_SCORE", "UNSCORABLE"}
            ]
            signals.append(self._trend(title, unit, values, as_of, source))

        cycles = self._session.scalars(
            select(WhoopCycle)
            .where(
                WhoopCycle.profile_id == profile_id,
                WhoopCycle.start_at >= start,
                WhoopCycle.start_at < today,
            )
            .limit(MAX_WEARABLE_ROWS)
        ).all()
        strain = [
            (row.start_at, float(row.strain), str(row.id))
            for row in cycles
            if row.strain is not None
            and 0 <= row.strain <= 21
            and row.score_state not in {"PENDING_SCORE", "UNSCORABLE"}
        ]
        signals.append(
            self._trend("Нагрузка WHOOP", None, strain, as_of, "whoop_strain")
        )
        return signals

    def _trend(
        self,
        title: str,
        unit: str | None,
        rows: Iterable[tuple[datetime, float, str]],
        as_of: datetime,
        source: str,
    ) -> HealthSignal:
        daily: dict[date, list[float]] = defaultdict(list)
        provenance: list[tuple[datetime, str]] = []
        today = datetime.combine(as_of.date(), datetime.min.time(), UTC)
        recent_start = today - timedelta(days=RECENT_DAYS)
        baseline_start = recent_start - timedelta(days=BASELINE_DAYS)
        for when, value, source_id in rows:
            when_utc = _utc(when)
            if baseline_start <= when_utc < today and isfinite(value):
                daily[when_utc.date()].append(value)
                provenance.append((when_utc, source_id))
        recent = [
            fmean(v)
            for d, v in daily.items()
            if recent_start.date() <= d < today.date()
        ]
        baseline = [
            fmean(v)
            for d, v in daily.items()
            if baseline_start.date() <= d < recent_start.date()
        ]
        citations = tuple(
            SourceCitation(
                f"[SNAP-{_citation_prefix(source)}-{index}]",
                source,
                source_id,
                when.date(),
            )
            for index, (when, source_id) in enumerate(
                sorted(provenance)[:MAX_SIGNAL_CITATIONS], 1
            )
        )
        if not citations:
            citations = (
                SourceCitation(
                    f"[SNAP-{_citation_prefix(source)}-NONE]", source, "none"
                ),
            )
        method = f"UTC-дни: {recent_start.date()}–{(today - timedelta(days=1)).date()} против {baseline_start.date()}–{(recent_start - timedelta(days=1)).date()}; среднее дневных средних"
        if len(recent) < MIN_RECENT_DAYS or len(baseline) < MIN_BASELINE_DAYS:
            return HealthSignal(
                SignalKind.WEARABLE,
                SignalState.GAP,
                title,
                f"Недостаточно полных дней: последние 7 — {len(recent)}, предыдущие 28 — {len(baseline)}. {method}",
                as_of,
                citations,
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
        detail = (
            f"Среднее за 7 полных UTC-дней {direction} среднего за предыдущие 28 дней"
        )
        if relative is not None:
            detail += f" на {abs(relative):.1f}%"
        detail += f". {method}"
        return HealthSignal(
            SignalKind.WEARABLE,
            SignalState.OBSERVED,
            title,
            detail,
            as_of,
            citations,
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


def _has_qualifier(source_value: str) -> bool:
    return source_value.lstrip().startswith(("<", ">", "≤", "≥"))


def _citation_prefix(source: str) -> str:
    return {
        "whoop_sleep": "SLEEP",
        "whoop_sleep_performance": "SLEEP-PERF",
        "whoop_recovery": "RECOVERY",
        "whoop_hrv": "HRV",
        "whoop_rhr": "RHR",
        "whoop_strain": "STRAIN",
    }.get(source, "WEARABLE")


def _explanation_key(canonical_name: str) -> str:
    return canonical_name.casefold().replace("-", "_").replace(" ", "_")


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
