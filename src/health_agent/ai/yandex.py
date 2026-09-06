"""Consent-gated Yandex AI Studio adapters over native Chat Completions."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from openai import APIStatusError, OpenAI

from health_agent.config import Settings
from health_agent.lab_extraction.openai import (
    _INSTRUCTIONS,
    _SCHEMA,
    _status_error_code,
)
from health_agent.lab_extraction.types import (
    MAX_CLOUD_CHARACTERS,
    Candidate,
    ExtractionError,
)
from health_agent.lab_extraction.validation import validate_candidates
from health_agent.questions.models import HealthQuestionContext
from health_agent.questions.openai import (
    MEDICAL_SAFETY_INSTRUCTIONS,
    build_responder_input,
)
from health_agent.questions.service import QuestionResponderError

YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"
MAX_CHAT_CONTENT_CHARACTERS = 80_000
_YANDEX_CITATION_INSTRUCTIONS = """
Citation output syntax is strict: copy each supplied bracketed citation label verbatim as a separate token. Never combine IDs inside brackets or abbreviate them into ranges. For multiple sources write [SLEEP1] [SLEEP2], only if both labels were supplied; never [SLEEP1, SLEEP2] or [SLEEP1–SLEEP2]. Square brackets are reserved exclusively for these exact evidence labels. Never put JSON field names, missing-data keys, placeholders, or explanatory text in square brackets. Explain missing data in ordinary Russian prose. Before finalizing, ensure every bracketed token occurs verbatim among the supplied citation labels. Do not invent evidence to satisfy the format.
"""


def _chat_content(response: Any) -> str:
    """Return one completed assistant message or a safe lab extraction code."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)) or len(choices) != 1:
        raise ExtractionError("cloud_invalid_output")
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason == "length":
        raise ExtractionError("cloud_incomplete")
    if finish_reason != "stop":
        raise ExtractionError("cloud_invalid_output")
    message = getattr(choice, "message", None)
    if message is None or getattr(message, "role", None) != "assistant":
        raise ExtractionError("cloud_invalid_output")
    if getattr(message, "refusal", None) is not None:
        raise ExtractionError("cloud_refused")
    if getattr(message, "tool_calls", None) or getattr(message, "function_call", None):
        raise ExtractionError("cloud_invalid_output")
    content = getattr(message, "content", None)
    if (
        not isinstance(content, str)
        or not content.strip()
        or len(content) > MAX_CHAT_CONTENT_CHARACTERS
    ):
        raise ExtractionError("cloud_invalid_output")
    return content.strip()


def _chat_question_messages(
    question: str, context: HealthQuestionContext
) -> list[dict[str, object]]:
    blocks = build_responder_input(question, context)[0]["content"]
    if not isinstance(blocks, list):  # pragma: no cover - builder contract guard
        raise TypeError("question content must be a list")
    return [
        {
            "role": "system",
            "content": MEDICAL_SAFETY_INSTRUCTIONS + _YANDEX_CITATION_INSTRUCTIONS,
        },
        {
            "role": "user",
            "content": [
                {**cast(dict[str, object], block), "type": "text"} for block in blocks
            ],
        },
    ]


def _component(value: str, label: str) -> str:
    value = value.strip()
    if not value or any(character in value for character in "/?#\r\n"):
        raise ValueError(f"Yandex {label} is invalid")
    return value


def yandex_model_uri(settings: Settings) -> str:
    """Resolve and locally validate the configured Yandex model URI."""
    folder = _component(settings.yandex_folder_id, "folder ID")
    model = _component(settings.yandex_model, "model")
    return f"gpt://{folder}/{model}"


def yandex_question_model_uri(settings: Settings) -> str:
    """Resolve the optional question model without changing extraction."""
    folder = _component(settings.yandex_folder_id, "folder ID")
    model = _component(
        (
            settings.yandex_question_model
            if settings.yandex_question_model is not None
            else settings.yandex_model
        ),
        "question model",
    )
    return f"gpt://{folder}/{model}"


class _YandexAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        client: Any = None,
        model: str | None = None,
        timeout_seconds: int = 30,
    ) -> None:
        self.settings = settings
        self._client = client
        self.model = model or yandex_model_uri(settings)
        self.timeout_seconds = timeout_seconds

    def _require_consent(self, profile_id: UUID) -> None:
        if profile_id not in self.settings.yandex_allowed_profile_ids:
            raise ExtractionError("cloud_provider_consent_required")

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                key = self.settings.load_yandex_api_key().get_secret_value()
                self._client = OpenAI(
                    api_key=key,
                    base_url=YANDEX_BASE_URL,
                    project=self.settings.yandex_folder_id.strip(),
                    timeout=float(self.timeout_seconds),
                    max_retries=0,
                    default_headers={"x-data-logging-enabled": "false"},
                )
            except Exception:  # noqa: BLE001 -- key/config details stay private
                raise ExtractionError("yandex_not_configured") from None
        return self._client


class YandexResponsesResponder(_YandexAdapter):
    """Historical name for the native Yandex Chat Completions responder."""

    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        super().__init__(
            settings,
            client=client,
            model=yandex_question_model_uri(settings),
            timeout_seconds=settings.yandex_question_timeout_seconds,
        )

    def respond(
        self,
        *,
        profile_id: UUID,
        question: str,
        context: HealthQuestionContext,
        request_id: str | None = None,
    ) -> str:
        try:
            self._require_consent(profile_id)
            options: dict[str, object] = {}
            if request_id is not None:
                options["extra_headers"] = {"X-Client-Request-Id": request_id}
            response = self._get_client().chat.completions.create(
                model=self.model,
                messages=_chat_question_messages(question, context),
                max_tokens=self.settings.openai_max_output_tokens,
                reasoning_effort="none",
                temperature=0,
                store=False,
                **options,
            )
        except Exception:  # noqa: BLE001 -- provider details stay private
            raise QuestionResponderError("Yandex responder unavailable") from None
        try:
            return _chat_content(response)
        except ExtractionError:
            raise QuestionResponderError("Yandex response was invalid") from None


class YandexLabExtractor(_YandexAdapter):
    def extract(self, profile_id: UUID, text: str) -> tuple[Candidate, ...]:
        self._require_consent(profile_id)
        if not text.strip() or len(text) > MAX_CLOUD_CHARACTERS:
            raise ExtractionError("cloud_input_limit")
        arguments = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": _INSTRUCTIONS},
                {"role": "user", "content": text},
            ],
            "max_tokens": self.settings.openai_max_output_tokens,
            "reasoning_effort": "none",
            "temperature": 0,
            "store": False,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "lab_candidates",
                    "strict": True,
                    "schema": _SCHEMA,
                },
            },
        }
        try:
            response = self._get_client().chat.completions.create(**arguments)
        except APIStatusError as error:
            raise ExtractionError(_status_error_code(error)) from None
        except ExtractionError:
            raise
        except Exception:  # noqa: BLE001 -- transport details stay private
            raise ExtractionError("cloud_outcome_unknown") from None
        output = _chat_content(response)
        try:
            return validate_candidates(json.loads(output), text)
        except (TypeError, ValueError):
            raise ExtractionError("cloud_invalid_output") from None
