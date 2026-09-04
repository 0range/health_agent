from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    LabObservation,
    Profile,
    ReviewStatus,
)
from health_agent.questions.context import (
    MAX_ITEMS_PER_SOURCE,
    build_context,
    detect_intent,
    window_days,
)
from health_agent.questions.models import EvidenceSource, QuestionIntent
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


def test_context_excludes_other_profiles_and_unverified_labs(session: Session) -> None:
    other_profile = Profile(id=uuid4(), name="Other")
    session.add(other_profile)
    session.flush()
    _lab(session, DEFAULT_PROFILE_ID, "Ferritin", 42, NOW.date())
    _lab(session, other_profile.id, "Ferritin", 999, NOW.date())
    _lab(session, DEFAULT_PROFILE_ID, "Vitamin D", 10, NOW.date(), ReviewStatus.NEEDS_REVIEW)

    context = build_context(session, DEFAULT_PROFILE_ID, "show my labs", clock=lambda: NOW)

    assert [(item.metric, item.value) for item in context.evidence] == [
        ("Ferritin", "42")
    ]
    assert context.source_counts[EvidenceSource.LAB] == 1
    assert context.evidence[0].citation_label == "[LAB1]"


def test_context_enforces_window_bounds_and_intent_windows(session: Session) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "Inside", 1, (NOW - timedelta(days=30)).date())
    _lab(session, DEFAULT_PROFILE_ID, "Outside", 2, (NOW - timedelta(days=31)).date())
    _lab(session, DEFAULT_PROFILE_ID, "Future", 3, (NOW + timedelta(days=1)).date())

    context = build_context(session, DEFAULT_PROFILE_ID, "general question", clock=lambda: NOW)

    assert [item.metric for item in context.evidence] == ["Inside"]
    assert context.window_start == NOW - timedelta(days=30)
    assert detect_intent("How was my sleep and recovery?") == QuestionIntent.SLEEP_RECOVERY
    assert window_days(QuestionIntent.SLEEP_RECOVERY) == 14
    assert detect_intent("Покажи динамику веса") == QuestionIntent.WEIGHT_TREND
    assert window_days(QuestionIntent.WEIGHT_TREND) == 90


def test_context_caps_and_labels_each_source_deterministically(session: Session) -> None:
    for index in range(MAX_ITEMS_PER_SOURCE + 2):
        _lab(
            session,
            DEFAULT_PROFILE_ID,
            f"Lab {index}",
            index,
            (NOW - timedelta(days=index)).date(),
        )

    context = build_context(session, DEFAULT_PROFILE_ID, "labs", clock=lambda: NOW)

    assert context.source_counts[EvidenceSource.LAB] == MAX_ITEMS_PER_SOURCE
    assert [item.citation_label for item in context.evidence] == [
        f"[LAB{index}]" for index in range(1, MAX_ITEMS_PER_SOURCE + 1)
    ]
    assert [item.metric for item in context.evidence] == [
        f"Lab {index}" for index in range(MAX_ITEMS_PER_SOURCE)
    ]


def test_context_uses_normalized_whoop_and_current_weight_without_raw_fields(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:cycles",)
    )
    _store_whoop(
        session,
        connection,
        "sleep",
        {
            "id": "sleep-1",
            "user_id": 7,
            "start": "2026-09-01T20:00:00Z",
            "end": "2026-09-02T05:00:00Z",
            "score": {
                "stage_summary": {
                    "total_light_sleep_time_milli": 14_400_000,
                    "total_slow_wave_sleep_time_milli": 7_200_000,
                    "total_rem_sleep_time_milli": 3_600_000,
                }
            },
        },
    )
    _store_whoop(
        session,
        connection,
        "recovery",
        {
            "cycle_id": 2,
            "user_id": 7,
            "updated_at": "2026-09-03T07:00:00Z",
            "score": {"recovery_score": 82},
        },
    )
    _store_whoop(
        session,
        connection,
        "body",
        {"height_meter": 1.8, "weight_kilogram": 74.5},
    )

    context = build_context(session, DEFAULT_PROFILE_ID, "weight trend", clock=lambda: NOW)

    assert [(item.source, item.value, item.unit) for item in context.evidence] == [
        (EvidenceSource.SLEEP, "7", "hours"),
        (EvidenceSource.RECOVERY, "82", "%"),
        (EvidenceSource.WEIGHT, "74.5", "kg"),
    ]
    assert [item.citation_label for item in context.evidence] == [
        "[SLEEP1]",
        "[RECOVERY1]",
        "[WEIGHT1]",
    ]
    assert not hasattr(context.evidence[0], "raw_record_id")
    assert not hasattr(context.evidence[0], "external_id")


def test_context_returns_empty_evidence_when_no_safe_records(session: Session) -> None:
    context = build_context(session, DEFAULT_PROFILE_ID, "anything", clock=lambda: NOW)

    assert context.evidence == ()
    assert context.source_counts == {source: 0 for source in EvidenceSource}


def _lab(
    session: Session,
    profile_id: UUID,
    name: str,
    value: int,
    issued_date: date,
    status: ReviewStatus = ReviewStatus.VERIFIED,
) -> None:
    document = Document(
        profile_id=profile_id,
        sha256=uuid4().hex + uuid4().hex,
        vault_path="private",
        media_type="application/pdf",
        document_type="lab",
        issued_date=issued_date,
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(document_id=document.id, page_number=1, extraction_method="text")
    )
    session.flush()
    session.add(
        LabObservation(
            document_id=document.id,
            page_number=1,
            canonical_name=name,
            source_name=name,
            source_value=str(value),
            parsed_value=Decimal(value),
            source_unit="u",
            normalized_value=Decimal(value),
            normalized_unit="u",
            evidence_excerpt="not returned by question context",
            confidence=Decimal(1),
            status=status,
        )
    )
    session.flush()


def _store_whoop(
    session: Session,
    connection: WhoopConnection,
    kind: str,
    payload: dict[str, object],
) -> None:
    record = normalize_whoop(kind, payload)
    store_normalized_record(
        session,
        connection,
        record,
        payload,
        NOW,
    )
