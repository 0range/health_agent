from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from health_agent.insights.models import (
    HealthSignal,
    HealthSnapshot,
    SignalKind,
    SignalState,
    SourceCitation,
)
from health_agent.questions.models import (
    ContextLimitation,
    ContextLimitationCode,
    EvidenceItem,
    EvidenceSource,
    HealthQuestionContext,
    QuestionIntent,
    SourceReport,
)
from health_agent.questions.service import (
    INSUFFICIENT_EVIDENCE_TEXT,
    QUESTION_UNAVAILABLE_TEXT,
    HealthQuestionApplicationService,
    QuestionAnswerErrorCode,
    render_source_footer,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)


class FakeContextBuilder:
    def __init__(self, context: HealthQuestionContext | None = None) -> None:
        self.context = context or _context()
        self.calls: list[tuple[UUID, str]] = []

    def build(self, profile_id: UUID, question: str) -> HealthQuestionContext:
        self.calls.append((profile_id, question))
        return self.context


class FakeResponder:
    def __init__(self, response: str = "Ferritin is recorded at 42. [LAB1]") -> None:
        self.response = response
        self.calls: list[tuple[UUID, str, HealthQuestionContext]] = []

    def respond(
        self,
        *,
        profile_id: UUID,
        question: str,
        context: HealthQuestionContext,
        request_id: str | None = None,
    ) -> str:
        self.calls.append((profile_id, question, context))
        return self.response


@pytest.mark.parametrize(
    "question",
    (
        "I have chest pain",
        "Что делать, не могу дышать?",
        "What should I do, I can't breathe?",
        "Как быть, хочу умереть?",
        "What should we do? He cannot breathe.",
    ),
)
def test_urgent_guard_precedes_context_retrieval_and_remote_responder(
    question: str,
) -> None:
    builder = FakeContextBuilder()
    responder = FakeResponder()

    result = HealthQuestionApplicationService(builder, responder).answer(
        PROFILE_ID, question
    )

    assert result.urgent is True
    assert result.available is True
    assert "экстренной помощи" in result.text.lower()
    assert builder.calls == []
    assert responder.calls == []


def test_answer_appends_deterministic_sources_and_structured_limitations() -> None:
    limitation = ContextLimitation(
        ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY,
        "Нельзя определить динамику по датам.",
    )
    context = _context(limitations=(limitation,))
    builder = FakeContextBuilder(context)
    responder = FakeResponder("Это наблюдение, а не диагноз. [LAB1]")

    result = HealthQuestionApplicationService(builder, responder).answer(
        PROFILE_ID, "What does my ferritin show?"
    )

    assert result.safe_error_code is None
    assert result.evidence == context.evidence
    assert result.limitations == (limitation,)
    assert result.text == (
        "Это наблюдение, а не диагноз. [LAB1]\n\n"
        "Источники:\n- [LAB1] 2026-09-03T09:00:00+00:00: Ferritin — 42 ug/L; "
        "референс источника: unknown\n\n"
        "Ограничения:\n- Нельзя определить динамику по датам."
    )
    assert responder.calls == [(PROFILE_ID, "What does my ferritin show?", context)]


def test_missing_evidence_is_local_and_does_not_call_responder() -> None:
    context = _context(evidence=())
    builder = FakeContextBuilder(context)
    responder = FakeResponder()

    result = HealthQuestionApplicationService(builder, responder).answer(
        PROFILE_ID, "How am I doing?"
    )

    assert result.text == INSUFFICIENT_EVIDENCE_TEXT
    assert result.safe_error_code is None
    assert responder.calls == []


def test_inference_blocking_limitation_is_local_even_with_current_evidence() -> None:
    limitation = ContextLimitation(
        ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY,
        "Нельзя определить динамику по датам.",
        prevents_requested_inference=True,
        prevents_entire_answer=True,
    )
    context = _context(intent=QuestionIntent.WEIGHT_TREND, limitations=(limitation,))
    responder = FakeResponder("Weight went down. [LAB1]")

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), responder
    ).answer(PROFILE_ID, "Has my weight changed over time?")

    assert result.text == (
        f"{INSUFFICIENT_EVIDENCE_TEXT}\n\n"
        "Ограничения:\n- Нельзя определить динамику по датам."
    )
    assert result.evidence == context.evidence
    assert result.limitations == (limitation,)
    assert responder.calls == []


def test_model_output_with_missing_or_forged_citations_fails_closed() -> None:
    context = _context()

    for generated in (
        "Ferritin is 42.",
        "Ferritin is 42. [LAB99]",
        "Ferritin is 42. [LAB1] [LAB99]",
        "Ferritin is 42. [LAB1] [FORGED]",
        "Ferritin is 42. [LAB1] [not-a-source]",
        f"Ferritin is 42. [LAB1] [{'X' * 65}]",
        "Ferritin is 42. [LAB1] [unfinished",
        "Ferritin is 42. [LAB1]]",
    ):
        responder = FakeResponder(generated)
        result = HealthQuestionApplicationService(
            FakeContextBuilder(context), responder
        ).answer(PROFILE_ID, "What does my ferritin show?")

        assert result.safe_error_code is None
        assert result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)
        assert "[LAB99]" not in result.text
        assert "Источники:" not in result.text
        assert len(responder.calls) == 1


def test_yandex_style_separate_citations_are_accepted_with_sources() -> None:
    context = _context(
        evidence=(
            EvidenceItem("[LAB1]", EvidenceSource.LAB, NOW, "Metric 1", "1", "u"),
            EvidenceItem("[LAB2]", EvidenceSource.LAB, NOW, "Metric 2", "2", "u"),
        )
    )

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), FakeResponder("Recorded fact. [LAB1] [LAB2]")
    ).answer(PROFILE_ID, "What is recorded?")

    assert not result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)
    assert "Источники:\n- [LAB1]" in result.text
    assert "\n- [LAB2]" in result.text


@pytest.mark.parametrize(
    "citations", ["[LAB1–LAB2]", "[LAB1, LAB2]", "[LAB1] [missing_keys]"]
)
def test_yandex_style_invalid_citation_formats_fail_closed(citations: str) -> None:
    context = _context(
        evidence=(
            EvidenceItem("[LAB1]", EvidenceSource.LAB, NOW, "Metric 1", "1", "u"),
            EvidenceItem("[LAB2]", EvidenceSource.LAB, NOW, "Metric 2", "2", "u"),
        )
    )

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), FakeResponder("Recorded fact. " + citations)
    ).answer(PROFILE_ID, "What is recorded?")

    assert result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)


def test_unselected_snapshot_citation_fails_closed_and_provenance_stays_internal() -> (
    None
):
    signals = tuple(_snapshot_signal(index) for index in range(31))
    context = _context()
    context = HealthQuestionContext(
        context.profile_id,
        context.intent,
        context.window_start,
        context.window_end,
        context.evidence,
        context.source_counts,
        snapshot=HealthSnapshot(PROFILE_ID, NOW, (), (), (), signals),
    )

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), FakeResponder("Omitted metric [S30-B].")
    ).answer(PROFILE_ID, "overview")

    assert result.text == INSUFFICIENT_EVIDENCE_TEXT
    assert len(context.snapshot.signals[0].citations) == 2


def test_footer_renders_one_cited_label_per_aggregate_and_no_unrelated_values() -> None:
    cited = _snapshot_signal(0)
    unrelated = _snapshot_signal(1)
    context = _context(evidence=())
    context = HealthQuestionContext(
        context.profile_id,
        context.intent,
        context.window_start,
        context.window_end,
        (),
        context.source_counts,
        snapshot=HealthSnapshot(PROFILE_ID, NOW, (), (), (), (cited, unrelated)),
    )

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), FakeResponder("Краткий ответ [S0-A].")
    ).answer(PROFILE_ID, "overview")

    assert result.text.count("- [S0-A]") == 1
    assert "[S0-B]" not in result.text
    assert "Metric 1" not in result.text


def test_pathological_citation_set_uses_explicit_overflow_summary() -> None:
    evidence = tuple(
        EvidenceItem(
            f"[LAB{index}]",
            EvidenceSource.LAB,
            NOW,
            f"Metric {index}",
            str(index),
            "u",
        )
        for index in range(10)
    )
    answer = "Кратко. " + " ".join(item.citation_label for item in evidence)
    result = HealthQuestionApplicationService(
        FakeContextBuilder(_context(evidence=evidence)), FakeResponder(answer)
    ).answer(PROFILE_ID, "overview")

    assert result.text.count("\n- [LAB") == 6
    assert "Ещё 4 процитированных источников указаны в ответе." in result.text


def test_context_and_responder_failures_are_stable_and_do_not_disclose_sensitive_data() -> (
    None
):
    secret = "sk-test-secret"
    question = "my medical question"
    evidence = "Ferritin 42"

    class BrokenBuilder(FakeContextBuilder):
        def build(self, profile_id: UUID, question: str) -> HealthQuestionContext:
            raise RuntimeError(f"{secret} {question} {evidence}")

    result = HealthQuestionApplicationService(BrokenBuilder(), FakeResponder()).answer(
        PROFILE_ID, question
    )
    assert result.text == QUESTION_UNAVAILABLE_TEXT
    assert result.safe_error_code is QuestionAnswerErrorCode.CONTEXT_UNAVAILABLE
    for unsafe in (secret, question, evidence):
        assert unsafe not in result.text

    class BrokenResponder(FakeResponder):
        def respond(
            self,
            *,
            profile_id: UUID,
            question: str,
            context: HealthQuestionContext,
            request_id: str | None = None,
        ) -> str:
            raise RuntimeError(f"{secret} {question} {evidence}")

    result = HealthQuestionApplicationService(
        FakeContextBuilder(), BrokenResponder()
    ).answer(PROFILE_ID, question)
    assert result.text == QUESTION_UNAVAILABLE_TEXT
    assert result.safe_error_code is QuestionAnswerErrorCode.RESPONDER_UNAVAILABLE
    for unsafe in (secret, question, evidence):
        assert unsafe not in result.text


def test_blank_question_is_rejected_before_context_or_remote_work() -> None:
    builder = FakeContextBuilder()
    responder = FakeResponder()

    result = HealthQuestionApplicationService(builder, responder).answer(
        PROFILE_ID, "  "
    )

    assert result.safe_error_code is QuestionAnswerErrorCode.INVALID_REQUEST
    assert result.text == QUESTION_UNAVAILABLE_TEXT
    assert builder.calls == []
    assert responder.calls == []


def test_report_footer_has_closed_source_identity_and_excludes_beyond_cap() -> None:
    document_id = UUID("10000000-0000-4000-8000-000000000001")
    visit_id = UUID("20000000-0000-4000-8000-000000000002")
    note_id = UUID("30000000-0000-4000-8000-000000000003")
    reports = (
        SourceReport(
            "[DOC1]",
            "document_excerpt",
            "Synthetic conclusion",
            f"document:{document_id}#page=7",
            date(2026, 9, 1),
            NOW,
        ),
        SourceReport(
            "[VISIT1]",
            "visit_answer",
            "Synthetic saved answer",
            f"visit:{visit_id}#note={note_id}",
            None,
            NOW,
        ),
        *tuple(
            SourceReport(
                f"[DOC{index}]",
                "document_excerpt",
                "Beyond selected cap",
                f"document:{UUID(int=index)}#page=1",
                None,
                NOW,
            )
            for index in range(2, 11)
        ),
    )
    context = _context(evidence=(), reports=reports)

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context),
        FakeResponder("Recorded wording [DOC1] [VISIT1]."),
    ).answer(PROFILE_ID, "What was reported?")
    rejected = HealthQuestionApplicationService(
        FakeContextBuilder(context), FakeResponder("Not selected [DOC10].")
    ).answer(PROFILE_ID, "What was reported?")

    assert f"источник document:{document_id}#page=7" in result.text
    assert f"источник visit:{visit_id}#note={note_id}" in result.text
    assert "Beyond selected cap" not in result.text
    assert rejected.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)


def test_mixed_footer_prioritizes_all_five_cited_document_locators() -> None:
    reports = tuple(_source_report(index) for index in range(1, 6))
    evidence = tuple(
        EvidenceItem(
            f"[WHOOP{index}]",
            EvidenceSource.WORKOUT,
            NOW,
            f"Telemetry {index}",
            str(index),
        )
        for index in range(1, 16)
    )
    context = _context(evidence=evidence, reports=reports)
    cited = {item.citation_label for item in (*reports, *evidence)}

    footer = render_source_footer(context, cited)

    assert all(report.source_reference in footer for report in reports)
    assert footer.count("document:") == 5
    assert "Ещё 14 процитированных источников" in footer


def test_ten_cited_reports_use_compact_tail_without_duplicate_text() -> None:
    reports = tuple(_source_report(index) for index in range(1, 11))
    context = _context(evidence=(), reports=reports)
    cited = {item.citation_label for item in reports}

    footer = render_source_footer(context, cited)

    assert footer.count("document:") == 10
    assert footer.count("Clinical text") == 6
    assert "Clinical text 7" not in footer


def test_invalid_report_locator_is_not_rendered() -> None:
    invalid = SourceReport(
        "[DOC1]",
        "document_excerpt",
        "Invalid locator text",
        "https://example.invalid/private.pdf",
        None,
        NOW,
    )
    footer = render_source_footer(_context(evidence=(), reports=(invalid,)), {"[DOC1]"})
    assert "example.invalid" not in footer


def _source_report(index: int) -> SourceReport:
    return SourceReport(
        f"[DOC{index}]",
        "document_excerpt",
        f"Clinical text {index}",
        f"document:{UUID(int=index)}#page={index}",
        None,
        NOW,
    )


def _context(
    *,
    evidence: tuple[EvidenceItem, ...] | None = None,
    limitations: tuple[ContextLimitation, ...] = (),
    intent: QuestionIntent = QuestionIntent.GENERAL,
    reports: tuple[SourceReport, ...] = (),
) -> HealthQuestionContext:
    return HealthQuestionContext(
        profile_id=PROFILE_ID,
        intent=intent,
        window_start=NOW,
        window_end=NOW,
        evidence=evidence
        if evidence is not None
        else (
            EvidenceItem(
                citation_label="[LAB1]",
                source=EvidenceSource.LAB,
                observed_at=datetime(2026, 9, 3, 9, tzinfo=UTC),
                metric="Ferritin",
                value="42",
                unit="ug/L",
            ),
        ),
        source_counts={EvidenceSource.LAB: 1},
        limitations=limitations,
        reports=reports,
    )


def _snapshot_signal(index: int) -> HealthSignal:
    return HealthSignal(
        SignalKind.WEARABLE,
        SignalState.OBSERVED,
        f"Metric {index}",
        f"Aggregate {index}",
        NOW,
        (
            SourceCitation(f"[S{index}-A]", "whoop", f"{index}-a"),
            SourceCitation(f"[S{index}-B]", "whoop", f"{index}-b"),
        ),
        value=str(index),
    )
