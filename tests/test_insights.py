from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from health_agent.insights.catalog import explain
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


def test_reviewed_catalogue_has_primary_sources_without_numeric_targets() -> None:
    for key in ("ferritin", "ldl_cholesterol", "vitamin_b12", "folate"):
        entry = explain(key)
        assert entry is not None
        assert entry.source_url.startswith("https://medlineplus.gov/lab-tests/")
        assert not any(mark in entry.general_knowledge for mark in ("<", ">", "≤", "≥"))


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

    assert signal.state is SignalState.GAP
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

    assert {
        signal.title for signal in snapshot.gaps if signal.kind is SignalKind.LAB
    } == {"No range", "Качество лабораторных данных"}
    assert "Draft" not in {signal.title for signal in snapshot.signals}
    assert not snapshot.stable


def test_empty_profile_has_explicit_unknown_lab_gap(session: Session) -> None:
    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )
    lab_gaps = [signal for signal in snapshot.gaps if signal.kind is SignalKind.LAB]
    assert len(lab_gaps) == 1
    assert "пока нет" in lab_gaps[0].summary


def test_rejected_or_future_only_profile_has_unknown_lab_gap(session: Session) -> None:
    _lab(
        session,
        DEFAULT_PROFILE_ID,
        "Rejected",
        day=NOW.date(),
        status=ReviewStatus.REJECTED,
    )
    _lab(
        session,
        DEFAULT_PROFILE_ID,
        "Future",
        day=NOW.date() + timedelta(days=1),
    )

    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )

    lab_gaps = [signal for signal in snapshot.gaps if signal.kind is SignalKind.LAB]
    assert len(lab_gaps) == 1
    assert "пока нет" in lab_gaps[0].summary
    assert not [signal for signal in snapshot.signals if signal.value is not None]


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


def test_latest_per_group_is_selected_before_global_bound(session: Session) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "Older distinct", day=date(2025, 1, 1))
    for index in range(201):
        _lab(
            session,
            DEFAULT_PROFILE_ID,
            "Repeated",
            day=date(2026, 1, 1) + timedelta(days=index),
        )

    snapshot = HealthSnapshotBuilder(session, clock=lambda: NOW).build(
        DEFAULT_PROFILE_ID
    )

    assert {"Older distinct", "Repeated"} <= {
        signal.title for signal in snapshot.signals
    }


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
        [(recent_day, 7.0, str(uuid4()))] * 20
        + [(baseline_day, 8.0, str(uuid4()))] * 20,
        NOW,
        "whoop_sleep",
    )

    assert signal.state is SignalState.GAP
    assert "последние 7 — 1" in signal.summary
    assert "предыдущие 28 — 1" in signal.summary


def test_wearable_windows_use_only_complete_utc_days(session: Session) -> None:
    builder = HealthSnapshotBuilder(session, clock=lambda: NOW)
    today_partial = datetime(2026, 9, 5, 1, tzinfo=UTC)
    day_seven = datetime(2026, 8, 29, 23, tzinfo=UTC)
    recent = [
        (day_seven + timedelta(days=offset), 10.0, str(uuid4())) for offset in range(7)
    ]
    baseline = [
        (
            datetime(2026, 8, 1, 12, tzinfo=UTC) + timedelta(days=offset),
            5.0,
            str(uuid4()),
        )
        for offset in range(28)
    ]

    signal = builder._trend(
        "Метрика",
        None,
        [*recent, *baseline, (today_partial, 999.0, str(uuid4()))],
        NOW,
        "whoop_test",
    )

    assert signal.state is SignalState.OBSERVED
    assert signal.value == "10.0"
    assert signal.citations[0].observed_on == date(2026, 8, 1)
    assert all(citation.source_id != "whoop_test" for citation in signal.citations)


def test_more_than_500_duplicate_records_select_same_latest_daily_value(
    session: Session,
) -> None:
    builder = HealthSnapshotBuilder(session, clock=lambda: NOW)
    duplicate_day = datetime(2026, 9, 4, tzinfo=UTC)
    duplicates = [
        (
            duplicate_day + timedelta(seconds=index),
            20.0 if index == 599 else 999.0,
            f"duplicate-{index:03}",
        )
        for index in range(600)
    ]
    recent = [
        (
            datetime(2026, 8, 29, 12, tzinfo=UTC) + timedelta(days=offset),
            20.0,
            f"recent-{offset}",
        )
        for offset in range(6)
    ]
    baseline = [
        (
            datetime(2026, 8, 1, 12, tzinfo=UTC) + timedelta(days=offset),
            10.0,
            f"baseline-{offset}",
        )
        for offset in range(28)
    ]
    rows = [*duplicates, *recent, *baseline]

    forward = builder._trend("Метрика", None, rows, NOW, "whoop_test")
    reverse = builder._trend("Метрика", None, reversed(rows), NOW, "whoop_test")

    assert forward.state is SignalState.OBSERVED
    assert forward.value == reverse.value == "20.0"
    assert forward.summary == reverse.summary
    assert forward.citations == reverse.citations
    assert len(forward.citations) == 35


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
