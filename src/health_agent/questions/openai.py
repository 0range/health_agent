"""OpenAI Responses adapter for bounded, profile-scoped health questions."""

from __future__ import annotations

from hashlib import sha256
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import SecretStr

from health_agent.questions.models import EvidenceItem, HealthQuestionContext
from health_agent.questions.service import QuestionResponderError

DEFAULT_OPENAI_MODEL = "gpt-5-mini"
DEFAULT_MAX_OUTPUT_TOKENS = 400
MAX_OUTPUT_TOKENS = 1_000
MAX_QUESTION_CHARACTERS = 4_000

MEDICAL_SAFETY_INSTRUCTIONS = """You are a careful health-information assistant.
Use only the supplied verified observations; do not invent, retrieve, or assume facts.
Do not diagnose, prescribe treatment, claim causality, or replace professional care.
Clearly distinguish recorded observations from tentative, non-diagnostic possibilities.
If the evidence cannot answer the question, say so plainly and suggest appropriate
follow-up with a qualified clinician. Do not provide emergency triage; the application
has already handled obvious emergency language. Cite supplied bracketed labels for every
data-dependent statement. Keep the answer concise and avoid repeating the full evidence."""


class ResponsesCreate(Protocol):
    def create(self, **kwargs: Any) -> object: ...


class ResponsesClient(Protocol):
    @property
    def responses(self) -> ResponsesCreate: ...


class OpenAIResponsesResponder:
    """A no-memory Responses API adapter with a test-injectable client."""

    def __init__(
        self,
        api_key: SecretStr | str,
        *,
        client: ResponsesClient | None = None,
        model: str = DEFAULT_OPENAI_MODEL,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        if not model.strip():
            raise ValueError("OpenAI model must not be empty")
        if not 1 <= max_output_tokens <= MAX_OUTPUT_TOKENS:
            raise ValueError("OpenAI max_output_tokens is out of bounds")
        key = (
            api_key.get_secret_value()
            if isinstance(api_key, SecretStr)
            else api_key
        )
        if not key.strip():
            raise ValueError("OpenAI API key is not configured")
        self._client = client or _build_openai_client(key)
        self._model = model
        self._max_output_tokens = max_output_tokens

    def respond(
        self, *, profile_id: UUID, question: str, context: HealthQuestionContext
    ) -> str:
        """Request one stateless response and reject incomplete/malformed output."""

        try:
            response = self._client.responses.create(
                model=self._model,
                instructions=MEDICAL_SAFETY_INSTRUCTIONS,
                input=build_responder_input(question, context),
                max_output_tokens=self._max_output_tokens,
                store=False,
                safety_identifier=hashed_safety_identifier(profile_id),
            )
        except Exception:  # noqa: BLE001 -- vendor errors must not cross this boundary
            raise QuestionResponderError("OpenAI responder unavailable") from None

        if getattr(response, "status", None) != "completed":
            raise QuestionResponderError("OpenAI response was not completed")
        output_text = getattr(response, "output_text", None)
        if not isinstance(output_text, str) or not output_text.strip():
            raise QuestionResponderError("OpenAI response was invalid")
        return output_text.strip()


def build_responder_input(question: str, context: HealthQuestionContext) -> str:
    """Create a bounded text-only prompt from typed context, never raw records."""

    bounded_question = question.strip()[:MAX_QUESTION_CHARACTERS]
    evidence_lines = [_evidence_prompt_line(item) for item in context.evidence]
    limitations = [limitation.message for limitation in context.limitations]
    return "\n".join(
        (
            "Question:",
            bounded_question,
            "",
            "Verified observations:",
            *(evidence_lines or ["No verified observations are available."]),
            "",
            "Known limitations:",
            *(limitations or ["None supplied."]),
        )
    )


def hashed_safety_identifier(profile_id: UUID) -> str:
    """Stable one-way identifier; never send the profile UUID to the provider."""

    return f"health-agent-{sha256(profile_id.bytes).hexdigest()}"


def _evidence_prompt_line(evidence: EvidenceItem) -> str:
    value = f"{evidence.value} {evidence.unit}" if evidence.unit else evidence.value
    return (
        f"{evidence.citation_label} | {evidence.observed_at.date().isoformat()} | "
        f"{evidence.metric} | {value}"
    )


def _build_openai_client(api_key: str) -> ResponsesClient:
    """Import lazily so injected fake clients never instantiate an SDK client."""

    from openai import OpenAI

    return cast(ResponsesClient, OpenAI(api_key=api_key))
