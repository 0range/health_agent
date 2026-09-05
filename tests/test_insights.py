from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from health_agent.insights.models import SignalKind, SignalState
from health_agent.insights.service import HealthSnapshotBuilder, _valid_bounds
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)

NOW = datetime(2026, 9, 5, 12, tzinfo=UTC)


def test_lab_uses_parsed_source_value_against_source_range_and_keeps_qualifier(
    session: Session,
) -> None:
    _lab(
        session,
        DEFAULT_PROFILE_ID,
        "Marker",
        source_value="<5",
        parsed=Decimal(5),
        normalized=Decimal(500),
        low=Decimal(10),
        high=Decimal(20),
        day=date(2025, 1, 2),
    )

    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )
    signal = next(item for item in snapshot.signals if item.kind is SignalKind.LAB)

    assert signal.state is SignalState.ATTENTION
    assert signal.value == "<5"
    assert signal.reference == "10–20"
    assert signal.observed_at.date() == date(2025, 1, 2)
    assert signal.citations[0].page_number == 1


def test_missing_range_is_gap_not_stable_and_unverified_is_excluded(
    session: Session,
) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "No range", day=NOW.date())
    _lab(
        session,
        DEFAULT_PROFILE_ID,
        "Draft",
        day=NOW.date(),
        status=ReviewStatus.NEEDS_REVIEW,
    )

    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )

    assert [
        signal.title for signal in snapshot.gaps if signal.kind is SignalKind.LAB
    ] == ["No range"]
    assert "Draft" not in {signal.title for signal in snapshot.signals}
    assert not snapshot.stable


def test_latest_per_analyte_and_source_unit_is_profile_scoped(session: Session) -> None:
    other = Profile(id=uuid4(), name="Other")
    session.add(other)
    session.flush()
    _lab(session, DEFAULT_PROFILE_ID, "Marker", day=date(2026, 1, 1), unit="mg/L")
    _lab(session, DEFAULT_PROFILE_ID, "Marker", day=date(2026, 2, 1), unit="mg/L")
    _lab(session, DEFAULT_PROFILE_ID, "Marker", day=date(2026, 1, 3), unit="mmol/L")
    _lab(session, other.id, "Other secret", day=NOW.date())

    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )

    labs = [signal for signal in snapshot.signals if signal.title == "Marker"]
    assert {signal.unit for signal in labs} == {"mg/L", "mmol/L"}
    assert {signal.observed_at.date() for signal in labs} == {
        date(2026, 2, 1),
        date(2026, 1, 3),
    }
    assert "Other secret" not in {signal.title for signal in snapshot.signals}


def test_nonfinite_and_inverted_bounds_are_invalid() -> None:
    assert not _valid_bounds(Decimal("NaN"), Decimal(2))
    assert not _valid_bounds(Decimal(2), Decimal(1))


def test_wearable_daily_grouping_prevents_duplicates_from_meeting_threshold(
    session: Session,
) -> None:
    builder = HealthSnapshotBuilder(session, clock=lambda: NOW)
    recent_day = NOW - timedelta(days=1)
    baseline_day = NOW - timedelta(days=10)

    signal = builder._trend(
        "Сон",
        "ч",
        [(recent_day, 7.0)] * 20 + [(baseline_day, 8.0)] * 20,
        NOW,
        "whoop_sleep",
    )

    assert signal.state is SignalState.GAP
    assert "последние 7 — 1" in signal.summary
    assert "предыдущие 28 — 1" in signal.summary


def _lab(
    session: Session,
    profile_id: UUID,
    name: str,
    *,
    source_value: str = "15",
    parsed: Decimal = Decimal(15),
    normalized: Decimal = Decimal(15),
    low: Decimal | None = None,
    high: Decimal | None = None,
    day: date,
    unit: str = "mg/L",
    status: ReviewStatus = ReviewStatus.VERIFIED,
) -> None:
    document = Document(
        profile_id=profile_id,
        sha256=uuid4().hex + uuid4().hex,
        vault_path="safe/test.pdf",
        media_type="application/pdf",
        document_type="lab",
        issued_date=day,
        collected_date=day,
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(
            document_id=document.id,
            page_number=1,
            extracted_text=None,
            extraction_method="test",
        )
    )
    session.flush()
    session.add(
        LabObservation(
            document_id=document.id,
            page_number=1,
            canonical_name=name,
            source_name=name,
            source_value=source_value,
            parsed_value=parsed,
            source_unit=unit,
            normalized_value=normalized,
            normalized_unit="normalized",
            reference_low=low,
            reference_high=high,
            evidence_excerpt="redacted",
            confidence=1,
            status=status,
        )
    )
    session.flush()
