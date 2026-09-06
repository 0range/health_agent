from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
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
    HealthContextBuilder,
    build_context,
    detect_intent,
    window_days,
)
from health_agent.questions.models import (
    ContextLimitationCode,
    EvidenceSource,
    EvidenceTimeSemantics,
    QuestionIntent,
)
from health_agent.questions.openai import build_responder_input
from health_agent.questions.service import HealthQuestionApplicationService
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    "question,intent,blocked",
    (
        ("Show my ferritin trend", QuestionIntent.GENERAL, False),
        ("Покажи динамику холестерина", QuestionIntent.GENERAL, False),
        ("What is my current weight?", QuestionIntent.CURRENT_WEIGHT, False),
        ("Какой сейчас вес?", QuestionIntent.CURRENT_WEIGHT, False),
        ("Has my weight changed?", QuestionIntent.WEIGHT_TREND, True),
        ("Покажи динамику веса", QuestionIntent.WEIGHT_TREND, True),
        ("How have my sleep and weight changed?", QuestionIntent.SLEEP_RECOVERY, False),
        ("Как изменились сон и вес?", QuestionIntent.SLEEP_RECOVERY, False),
    ),
)
def test_inference_is_separate_from_window_selection(
    session: Session, question: str, intent: QuestionIntent, blocked: bool
) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "Ferritin", 42, NOW.date())
    _lab(session, DEFAULT_PROFILE_ID, "Ferritin", 40, (NOW - timedelta(days=3)).date())
    connection = register_authorized_connection(
        session,
        DEFAULT_PROFILE_ID,
        "primary",
        7,
        ("read:body_measurement", "read:sleep"),
    )
    _store_whoop(session, connection, "body", {"weight_kilogram": 74.5})
    _store_whoop(
        session,
        connection,
        "sleep",
        {
            "id": "sleep-1",
            "user_id": 7,
            "cycle_id": 2,
            "start": "2026-09-03T20:00:00Z",
            "score": {"stage_summary": {"total_light_sleep_time_milli": 25_200_000}},
        },
    )
    calls = []

    def respond(**kwargs):
        calls.append(kwargs)
        if intent is QuestionIntent.SLEEP_RECOVERY:
            return "The recorded sleep was 7 hours [SLEEP1]."
        return "The current verified value is recorded [LAB1]."

    builder = HealthContextBuilder(session, clock=lambda: NOW)
    context = builder.build(DEFAULT_PROFILE_ID, question)
    result = HealthQuestionApplicationService(
        builder, SimpleNamespace(respond=respond)
    ).answer(DEFAULT_PROFILE_ID, question)

    assert context.intent is intent
    assert bool(calls) is not blocked
    assert context.source_counts[EvidenceSource.LAB] == 2
    if intent in {QuestionIntent.SLEEP_RECOVERY, QuestionIntent.WEIGHT_TREND}:
        assert context.limitations[0].prevents_requested_inference
        assert context.limitations[0].prevents_entire_answer is blocked
        assert "нельзя определить изменение веса" in result.text
    else:
        assert context.limitations == ()
    if intent is QuestionIntent.SLEEP_RECOVERY:
        assert result.text.startswith("The recorded sleep was 7 hours")


@pytest.mark.parametrize(
    "offset,included", ((-30, True), (0, True), (-31, False), (1, False))
)
def test_current_weight_enforces_both_inclusive_sync_window_bounds(
    session: Session, offset: int, included: bool
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:body_measurement",)
    )
    _store_whoop(
        session,
        connection,
        "body",
        {"weight_kilogram": 74.5},
        fetched_at=NOW + timedelta(days=offset),
    )
    context = build_context(
        session, DEFAULT_PROFILE_ID, "current weight", clock=lambda: NOW
    )
    assert bool(context.evidence) is included
    assert not context.limitations


def test_context_excludes_other_profiles_and_unverified_labs(session: Session) -> None:
    other_profile = Profile(id=uuid4(), name="Other")
    session.add(other_profile)
    session.flush()
    _lab(session, DEFAULT_PROFILE_ID, "Ferritin", 42, NOW.date())
    _lab(session, other_profile.id, "Ferritin", 999, NOW.date())
    _lab(
        session,
        DEFAULT_PROFILE_ID,
        "Vitamin D",
        10,
        NOW.date(),
        ReviewStatus.NEEDS_REVIEW,
    )

    context = build_context(
        session, DEFAULT_PROFILE_ID, "show my labs", clock=lambda: NOW
    )

    assert [(item.metric, item.value) for item in context.evidence] == [
        ("Ferritin", "42")
    ]
    assert context.source_counts[EvidenceSource.LAB] == 1
    assert context.evidence[0].citation_label == "[LAB1]"


def test_context_enforces_window_bounds_and_intent_windows(session: Session) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "Inside", 1, (NOW - timedelta(days=30)).date())
    _lab(session, DEFAULT_PROFILE_ID, "Outside", 2, (NOW - timedelta(days=31)).date())
    _lab(session, DEFAULT_PROFILE_ID, "Future", 3, (NOW + timedelta(days=1)).date())

    context = build_context(
        session, DEFAULT_PROFILE_ID, "general question", clock=lambda: NOW
    )

    assert [item.metric for item in context.evidence] == ["Inside"]
    assert context.window_start == NOW - timedelta(days=30)
    assert (
        detect_intent("How was my sleep and recovery?") == QuestionIntent.SLEEP_RECOVERY
    )
    assert window_days(QuestionIntent.SLEEP_RECOVERY) == 14
    assert detect_intent("Покажи динамику веса") == QuestionIntent.WEIGHT_TREND
    assert window_days(QuestionIntent.WEIGHT_TREND) == 90


def test_old_snapshot_lab_flows_prompt_validator_and_footer(session: Session) -> None:
    _lab(session, DEFAULT_PROFILE_ID, "Old marker", 15, date(2025, 1, 1))
    builder = HealthContextBuilder(session, clock=lambda: NOW)
    context = builder.build(DEFAULT_PROFILE_ID, "Что было в старых анализах?")

    assert not context.evidence
    assert context.snapshot is not None
    payload = build_responder_input("Что было в старых анализах?", context)[0]
    content = cast(list[dict[str, str]], payload["content"])
    snapshot_data = json.loads(content[1]["text"])["health_snapshot"]
    lab_signal = next(
        signal for signal in snapshot_data["signals"] if signal["title"] == "Old marker"
    )
    assert lab_signal["citation_ids"] == ["[SNAP1]"]

    accepted = HealthQuestionApplicationService(
        builder, SimpleNamespace(respond=lambda **_: "Есть старый результат [SNAP1].")
    ).answer(DEFAULT_PROFILE_ID, "Что было в старых анализах?")
    rejected = HealthQuestionApplicationService(
        builder, SimpleNamespace(respond=lambda **_: "Выдуманный результат [SNAP999].")
    ).answer(DEFAULT_PROFILE_ID, "Что было в старых анализах?")

    assert accepted.text == "Есть старый результат."
    assert "Источники:" not in accepted.text
    assert "[SNAP1]" not in accepted.text
    assert rejected.text.startswith("В выбранном периоде недостаточно")


def test_context_caps_and_labels_each_source_deterministically(
    session: Session,
) -> None:
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
            "cycle_id": 2,
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
            "sleep_id": "sleep-1",
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

    context = build_context(
        session, DEFAULT_PROFILE_ID, "weight trend", clock=lambda: NOW
    )

    assert [(item.source, item.value, item.unit) for item in context.evidence] == [
        (EvidenceSource.SLEEP, "7", "ч"),
        (EvidenceSource.RECOVERY, "82", "%"),
        (EvidenceSource.WEIGHT, "74.5", "кг"),
    ]
    assert [item.citation_label for item in context.evidence] == [
        "[SLEEP1]",
        "[RECOVERY1]",
        "[WEIGHT1]",
    ]
    assert not hasattr(context.evidence[0], "raw_record_id")
    assert not hasattr(context.evidence[0], "external_id")
    weight = next(
        item for item in context.evidence if item.source is EvidenceSource.WEIGHT
    )
    assert weight.metric == "Текущий вес WHOOP (на момент синхронизации)"
    assert weight.time_semantics is EvidenceTimeSemantics.SYNC_AS_OF
    assert (
        context.limitations[0].code
        is ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY
    )


def test_recovery_uses_associated_physiological_time_not_update_time(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:recovery",)
    )
    _store_whoop(
        session,
        connection,
        "sleep",
        {
            "id": "old-sleep",
            "user_id": 7,
            "start": "2026-08-01T22:00:00Z",
            "score": {},
        },
    )
    _store_whoop(
        session,
        connection,
        "recovery",
        {
            "cycle_id": 1,
            "sleep_id": "old-sleep",
            "user_id": 7,
            "updated_at": "2026-09-04T11:00:00Z",
            "score": {"recovery_score": 10},
        },
    )
    _store_whoop(
        session,
        connection,
        "sleep",
        {
            "id": "recent-sleep",
            "user_id": 7,
            "start": "2026-09-03T22:00:00Z",
            "score": {},
        },
    )
    _store_whoop(
        session,
        connection,
        "recovery",
        {
            "cycle_id": 2,
            "sleep_id": "recent-sleep",
            "user_id": 7,
            "updated_at": "2026-08-01T11:00:00Z",
            "score": {"recovery_score": 80},
        },
    )

    context = build_context(session, DEFAULT_PROFILE_ID, "recovery", clock=lambda: NOW)

    recoveries = [
        item for item in context.evidence if item.source is EvidenceSource.RECOVERY
    ]
    assert [(item.value, item.observed_at) for item in recoveries] == [
        ("80", datetime(2026, 9, 3, 22, tzinfo=UTC))
    ]


def test_recovery_join_is_profile_and_connection_scoped(session: Session) -> None:
    other_profile = Profile(id=uuid4(), name="Other")
    session.add(other_profile)
    session.flush()
    primary = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:recovery",)
    )
    other = register_authorized_connection(
        session, other_profile.id, "other", 8, ("read:sleep",)
    )
    _store_whoop(
        session,
        primary,
        "recovery",
        {
            "cycle_id": 1,
            "sleep_id": "shared-sleep",
            "user_id": 7,
            "score": {"recovery_score": 50},
        },
    )
    _store_whoop(
        session,
        other,
        "sleep",
        {
            "id": "shared-sleep",
            "user_id": 8,
            "start": "2026-09-03T22:00:00Z",
            "score": {},
        },
    )

    context = build_context(session, DEFAULT_PROFILE_ID, "recovery", clock=lambda: NOW)

    assert not [
        item for item in context.evidence if item.source is EvidenceSource.RECOVERY
    ]


def test_recovery_falls_back_to_the_associated_cycle_time(session: Session) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:recovery",)
    )
    _store_whoop(
        session,
        connection,
        "cycle",
        {
            "id": 2,
            "user_id": 7,
            "start": "2026-09-03T08:00:00Z",
            "score": {},
        },
    )
    _store_whoop(
        session,
        connection,
        "recovery",
        {
            "cycle_id": 2,
            "user_id": 7,
            "updated_at": "2026-08-01T11:00:00Z",
            "score": {"recovery_score": 70},
        },
    )

    context = build_context(session, DEFAULT_PROFILE_ID, "recovery", clock=lambda: NOW)

    recovery = next(
        item for item in context.evidence if item.source is EvidenceSource.RECOVERY
    )
    assert recovery.observed_at == datetime(2026, 9, 3, 8, tzinfo=UTC)


@pytest.mark.parametrize("offset", (-180, 1))
def test_weight_trend_excludes_stale_and_future_body_snapshots(
    session: Session, offset: int
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "primary", 7, ("read:body_measurement",)
    )
    stale_sync = NOW + timedelta(days=offset)
    _store_whoop(
        session,
        connection,
        "body",
        {"height_meter": 1.8, "weight_kilogram": 74.5},
        fetched_at=stale_sync,
    )

    context = build_context(
        session, DEFAULT_PROFILE_ID, "weight trend", clock=lambda: NOW
    )

    assert not any(item.source is EvidenceSource.WEIGHT for item in context.evidence)
    assert len(context.limitations) == 1
    assert (
        context.limitations[0].code
        is ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY
    )


def test_lab_prefers_collection_date_for_window_and_citation(session: Session) -> None:
    document = Document(
        profile_id=DEFAULT_PROFILE_ID,
        sha256=uuid4().hex + uuid4().hex,
        vault_path="private",
        media_type="application/pdf",
        document_type="lab",
        collected_date=(NOW - timedelta(days=30)).date(),
        issued_date=(NOW - timedelta(days=31)).date(),
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
            canonical_name="Ferritin",
            source_name="Ferritin",
            source_value="42",
            parsed_value=Decimal(42),
            source_unit="u",
            normalized_value=Decimal(42),
            normalized_unit="u",
            evidence_excerpt="not returned by question context",
            confidence=Decimal(1),
            status=ReviewStatus.VERIFIED,
        )
    )
    session.flush()

    context = build_context(session, DEFAULT_PROFILE_ID, "labs", clock=lambda: NOW)

    assert context.evidence[0].observed_at == datetime(2026, 8, 5, tzinfo=UTC)


def test_legacy_lab_source_display_and_unknown_reference_reach_real_prompt(
    session: Session,
) -> None:
    document = Document(
        profile_id=DEFAULT_PROFILE_ID,
        sha256=uuid4().hex + uuid4().hex,
        vault_path="private",
        media_type="application/pdf",
        document_type="lab",
        collected_date=NOW.date(),
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(document_id=document.id, page_number=1, extraction_method="text")
    )
    session.add_all(
        [
            LabObservation(
                document_id=document.id,
                page_number=1,
                canonical_name="Qualified",
                source_name="Qualified",
                source_value="<5",
                parsed_value=Decimal(5),
                source_unit="mg/L",
                normalized_value=Decimal("0.5"),
                normalized_unit="g/L",
                reference_text="5–10 mg/L",
                evidence_excerpt="synthetic",
                confidence=Decimal(1),
                status=ReviewStatus.VERIFIED,
            ),
            LabObservation(
                document_id=document.id,
                page_number=1,
                canonical_name="No reference",
                source_name="No reference",
                source_value="7",
                parsed_value=Decimal(7),
                source_unit="U/L",
                normalized_value=Decimal(7),
                normalized_unit="U/L",
                evidence_excerpt="synthetic",
                confidence=Decimal(1),
                status=ReviewStatus.VERIFIED,
            ),
        ]
    )
    session.flush()

    context = build_context(session, DEFAULT_PROFILE_ID, "labs", clock=lambda: NOW)
    message = build_responder_input("labs", context)[0]
    contents = cast(list[dict[str, str]], message["content"])
    observations = json.loads(contents[1]["text"])["verified_observations"]
    by_metric = {item["metric"]: item for item in observations}

    assert by_metric["Qualified"]["source_value"] == "<5"
    assert by_metric["Qualified"]["source_unit"] == "mg/L"
    assert by_metric["Qualified"]["value"] == "0.5"
    assert by_metric["Qualified"]["unit"] == "g/L"
    assert by_metric["Qualified"]["source_reference"] == "5–10 mg/L"
    assert by_metric["No reference"]["source_reference"] == "unknown"


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
    *,
    fetched_at: datetime = NOW,
) -> None:
    record = normalize_whoop(kind, payload)
    store_normalized_record(
        session,
        connection,
        record,
        payload,
        fetched_at,
    )
