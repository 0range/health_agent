from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from health_agent.questions.models import (
    ContextLimitation,
    ContextLimitationCode,
    EvidenceItem,
    EvidenceSource,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.questions.service import (
    INSUFFICIENT_EVIDENCE_TEXT,
    QUESTION_UNAVAILABLE_TEXT,
    HealthQuestionApplicationService,
    QuestionAnswerErrorCode,
)

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")
NOW = datetime(2026, 9, 4, 12, tzinfo=UTC)
SELECTION = (
    "Выбранный период (включительно, UTC): 2026-09-04T12:00:00+00:00 — "
    "2026-09-04T12:00:00+00:00; не более 10 записей из каждого источника.\n"
    "Для анализов указана календарная дата; для WHOOP — время наблюдения "
    "или синхронизации.\n"
)


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
        self, *, profile_id: UUID, question: str, context: HealthQuestionContext,
        request_id: str | None = None,
    ) -> str:
        self.calls.append((profile_id, question, context))
        return self.response


@pytest.mark.parametrize("question", (
    "I have chest pain", "Что делать, не могу дышать?",
    "What should I do, I can't breathe?", "Как быть, хочу умереть?",
    "What should we do? He cannot breathe.",
))
def test_urgent_guard_precedes_context_retrieval_and_remote_responder(question: str) -> None:
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
        f"Источники:\n{SELECTION}- [LAB1] 2026-09-03T09:00:00+00:00: Ferritin — 42 ug/L\n\n"
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

    assert result.text == (
        f"{INSUFFICIENT_EVIDENCE_TEXT}\n\nИсточники:\n{SELECTION}"
        "- В выбранном периоде нет проверенных данных."
    )
    assert result.safe_error_code is None
    assert responder.calls == []


def test_inference_blocking_limitation_is_local_even_with_current_evidence() -> None:
    limitation = ContextLimitation(
        ContextLimitationCode.WEIGHT_TREND_INSUFFICIENT_HISTORY,
        "Нельзя определить динамику по датам.",
        prevents_requested_inference=True,
        prevents_entire_answer=True,
    )
    context = _context(
        intent=QuestionIntent.WEIGHT_TREND, limitations=(limitation,)
    )
    responder = FakeResponder("Weight went down. [LAB1]")

    result = HealthQuestionApplicationService(
        FakeContextBuilder(context), responder
    ).answer(PROFILE_ID, "Has my weight changed over time?")

    assert result.text == (
        f"{INSUFFICIENT_EVIDENCE_TEXT}\n\nИсточники:\n{SELECTION}"
        "- [LAB1] 2026-09-03T09:00:00+00:00: Ferritin — 42 ug/L\n\n"
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
        assert "- [LAB1] 2026-09-03T09:00:00+00:00: Ferritin — 42 ug/L" in result.text
        assert len(responder.calls) == 1


def test_context_and_responder_failures_are_stable_and_do_not_disclose_sensitive_data() -> None:
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
            self, *, profile_id: UUID, question: str, context: HealthQuestionContext,
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

    result = HealthQuestionApplicationService(builder, responder).answer(PROFILE_ID, "  ")

    assert result.safe_error_code is QuestionAnswerErrorCode.INVALID_REQUEST
    assert result.text == QUESTION_UNAVAILABLE_TEXT
    assert builder.calls == []
    assert responder.calls == []


def _context(
    *,
    evidence: tuple[EvidenceItem, ...] | None = None,
    limitations: tuple[ContextLimitation, ...] = (),
    intent: QuestionIntent = QuestionIntent.GENERAL,
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
    )
