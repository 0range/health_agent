from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.questions.models import (
    EvidenceItem,
    EvidenceSource,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.questions.openai import (
    DEFAULT_OPENAI_MODEL,
    MEDICAL_SAFETY_INSTRUCTIONS,
    OpenAIResponsesResponder,
    build_responder_input,
    hashed_safety_identifier,
)
from health_agent.questions.service import QuestionResponderError

PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")


class FakeResponses:
    def __init__(self, result: object | BaseException) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def create(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_responses_adapter_uses_exact_stateless_safe_call_arguments() -> None:
    responses = FakeResponses(SimpleNamespace(status="completed", output_text="Safe. [LAB1]"))
    client = SimpleNamespace(responses=responses)
    context = _context()
    responder = OpenAIResponsesResponder(
        "not-a-real-key", client=client, max_output_tokens=222
    )

    answer = responder.respond(
        profile_id=PROFILE_ID, question="What does this mean?", context=context
    )

    assert answer == "Safe. [LAB1]"
    assert responses.calls == [
        {
            "model": DEFAULT_OPENAI_MODEL,
            "instructions": MEDICAL_SAFETY_INSTRUCTIONS,
            "input": build_responder_input("What does this mean?", context),
            "max_output_tokens": 222,
            "store": False,
            "safety_identifier": hashed_safety_identifier(PROFILE_ID),
        }
    ]
    assert str(PROFILE_ID) not in responses.calls[0]["safety_identifier"]
    assert "previous_response_id" not in responses.calls[0]
    assert "conversation" not in responses.calls[0]


@pytest.mark.parametrize(
    "response",
    (
        SimpleNamespace(status="incomplete", output_text="partial"),
        SimpleNamespace(status="completed", output_text=""),
        SimpleNamespace(status="completed", output_text=None),
        object(),
    ),
)
def test_responses_adapter_rejects_noncompleted_empty_or_malformed_response(
    response: object,
) -> None:
    responder = OpenAIResponsesResponder(
        "not-a-real-key", client=SimpleNamespace(responses=FakeResponses(response))
    )

    with pytest.raises(QuestionResponderError) as caught:
        responder.respond(profile_id=PROFILE_ID, question="question", context=_context())

    assert "question" not in str(caught.value)


def test_responses_exception_is_sanitized() -> None:
    sensitive = "sk-private medical evidence"
    responder = OpenAIResponsesResponder(
        "not-a-real-key",
        client=SimpleNamespace(responses=FakeResponses(RuntimeError(sensitive))),
    )

    with pytest.raises(QuestionResponderError) as caught:
        responder.respond(profile_id=PROFILE_ID, question="question", context=_context())

    assert sensitive not in str(caught.value)
    assert caught.value.__cause__ is None


def test_prompt_is_bounded_and_medically_constrained() -> None:
    prompt = build_responder_input("q" * 5_000, _context())

    assert "Question:" in prompt
    assert "[LAB1] | 2026-09-03 | Ferritin | 42 ug/L" in prompt
    assert "q" * 4_000 in prompt
    assert "q" * 4_001 not in prompt
    assert "do not diagnose" in MEDICAL_SAFETY_INSTRUCTIONS.lower()
    assert "only the supplied verified observations" in MEDICAL_SAFETY_INSTRUCTIONS.lower()


def test_safety_identifier_is_one_way_stable_and_profile_specific() -> None:
    first = hashed_safety_identifier(PROFILE_ID)
    second = hashed_safety_identifier(UUID("00000000-0000-0000-0000-000000000002"))

    assert first == hashed_safety_identifier(PROFILE_ID)
    assert first != second
    assert str(PROFILE_ID) not in first
    assert len(first.removeprefix("health-agent-")) == 64


def _context() -> HealthQuestionContext:
    return HealthQuestionContext(
        profile_id=PROFILE_ID,
        intent=QuestionIntent.GENERAL,
        window_start=datetime(2026, 9, 1, tzinfo=UTC),
        window_end=datetime(2026, 9, 4, tzinfo=UTC),
        evidence=(
            EvidenceItem(
                citation_label="[LAB1]",
                source=EvidenceSource.LAB,
                observed_at=datetime(2026, 9, 3, tzinfo=UTC),
                metric="Ferritin",
                value="42",
                unit="ug/L",
            ),
        ),
        source_counts={EvidenceSource.LAB: 1},
    )
