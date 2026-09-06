"""Consent-gated Yandex AI Studio adapters over its Responses endpoint."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import UUID

from openai import APIStatusError, OpenAI

from health_agent.config import Settings
from health_agent.lab_extraction.openai import (
    _INSTRUCTIONS,
    _SCHEMA,
    _status_error_code,
    parse_lab_response,
)
from health_agent.lab_extraction.types import (
    MAX_CLOUD_CHARACTERS,
    Candidate,
    ExtractionError,
)
from health_agent.questions.models import HealthQuestionContext
from health_agent.questions.openai import (
    MEDICAL_SAFETY_INSTRUCTIONS,
    build_responder_input,
    hashed_safety_identifier,
)
from health_agent.questions.service import QuestionResponderError

YANDEX_BASE_URL = "https://ai.api.cloud.yandex.net/v1"


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


class _YandexAdapter:
    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self.settings = settings
        self._client = client
        self.model = yandex_model_uri(settings)

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
                    timeout=30.0,
                    max_retries=0,
                    default_headers={"x-data-logging-enabled": "false"},
                )
            except Exception:  # noqa: BLE001 -- key/config details stay private
                raise ExtractionError("yandex_not_configured") from None
        return self._client


class YandexResponsesResponder(_YandexAdapter):
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
            response = self._get_client().responses.create(
                model=self.model,
                instructions=MEDICAL_SAFETY_INSTRUCTIONS,
                input=build_responder_input(question, context),
                max_output_tokens=self.settings.openai_max_output_tokens,
                store=False,
                safety_identifier=hashed_safety_identifier(profile_id),
                **options,
            )
        except Exception:  # noqa: BLE001 -- provider details stay private
            raise QuestionResponderError("Yandex responder unavailable") from None
        if getattr(response, "status", None) != "completed":
            raise QuestionResponderError("Yandex response was not completed")
        output = getattr(response, "output_text", None)
        if not isinstance(output, str) or not output.strip():
            raise QuestionResponderError("Yandex response was invalid")
        return output.strip()


class YandexLabExtractor(_YandexAdapter):
    def extract(self, profile_id: UUID, text: str) -> tuple[Candidate, ...]:
        self._require_consent(profile_id)
        if not text.strip() or len(text) > MAX_CLOUD_CHARACTERS:
            raise ExtractionError("cloud_input_limit")
        arguments = {
            "model": self.model,
            "instructions": _INSTRUCTIONS,
            "input": json.dumps({"page_text": text}, ensure_ascii=False),
            "max_output_tokens": self.settings.openai_max_output_tokens,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "lab_candidates",
                    "strict": True,
                    "schema": _SCHEMA,
                }
            },
            "safety_identifier": hashlib.sha256(
                b"health-agent-lab-extraction-v1:" + profile_id.bytes
            ).hexdigest(),
        }
        try:
            response = self._get_client().responses.create(**arguments)
        except APIStatusError as error:
            raise ExtractionError(_status_error_code(error)) from None
        except ExtractionError:
            raise
        except Exception:  # noqa: BLE001 -- transport details stay private
            raise ExtractionError("cloud_outcome_unknown") from None
        return parse_lab_response(response, text)
