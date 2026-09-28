"""Bounded OpenAI Platform text, image and voice calls for the pilot coaches."""

import math
import time
from collections.abc import Callable
from hashlib import sha256
from typing import Any
from uuid import UUID

from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI

from health_agent.config import Settings


class PilotProviderError(ValueError):
    """Allowlisted local diagnosis; never retains vendor messages or credentials."""

    def __init__(self, code: str, *, status: int | None = None,
                 retry_delay: float | None = None, attempts: int = 1) -> None:
        super().__init__(code)
        self.code, self.status = code, status
        self.retry_delay, self.attempts = retry_delay, attempts

    def metadata(self) -> dict[str, Any]:
        return {"safe_error_code": self.code, "http_status": self.status,
                "attempts": self.attempts}


def _failure(error: Exception) -> PilotProviderError:
    if isinstance(error, PilotProviderError):
        return error
    if isinstance(error, APITimeoutError):
        return PilotProviderError("pilot_provider_timeout", retry_delay=0.5)
    if isinstance(error, APIConnectionError):
        return PilotProviderError("pilot_provider_connection_error", retry_delay=0.5)
    if not isinstance(error, APIStatusError):
        return PilotProviderError("pilot_provider_unavailable")
    status = error.status_code
    billing_codes = {"insufficient_quota", "credit_balance_exhausted",
                     "organization_spend_limit_exceeded", "project_spend_limit_exceeded",
                     "organization_usage_limit_exceeded"}
    if status == 429 and getattr(error, "code", None) in billing_codes:
        return PilotProviderError("pilot_provider_quota_exhausted", status=status)
    if status in {401, 403}:
        return PilotProviderError("pilot_provider_auth_required", status=status)
    retryable = status in {408, 409, 429} or status >= 500
    code = ("pilot_provider_server_error" if status >= 500 else
            "pilot_provider_rate_limited" if status == 429 else
            "pilot_provider_transient_error" if retryable else "pilot_provider_request_rejected")
    delay = 0.5 if retryable else None
    retry_after = error.response.headers.get("retry-after")
    if retryable and retry_after is not None:
        try:
            requested = float(retry_after)
            # A longer or unrecognized Retry-After defers to a later user retry;
            # do not ignore it or hold the Telegram worker indefinitely.
            delay = max(0.5, requested) if math.isfinite(requested) and 0 <= requested <= 2 else None
        except ValueError:
            delay = None
    return PilotProviderError(code, status=status, retry_delay=delay)


def _bounded_call(call: Callable[[], Any]) -> Any:
    for attempt in (1, 2):
        try:
            return call()
        except Exception as error:  # noqa: BLE001 -- normalize SDK errors before logging
            failure = _failure(error)
            failure.attempts = attempt
            if attempt == 1 and failure.retry_delay is not None:
                time.sleep(failure.retry_delay)
                continue
            raise failure from None


class _OpenAIAdapter:
    def __init__(self, settings: Settings, *, client: Any = None) -> None:
        self.settings = settings
        self._client = client

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                self._client = OpenAI(
                    api_key=self.settings.load_openai_api_key().get_secret_value(),
                    base_url="https://api.openai.com/v1",
                    timeout=60.0,
                    max_retries=0,
                )
            except Exception:  # noqa: BLE001 -- SDK/key details must remain private
                raise PilotProviderError("pilot_provider_unavailable") from None
        return self._client

    def respond(
        self, system: str, content: list[dict[str, Any]], profile_id: UUID,
    ) -> tuple[Any, str]:
        blocks = [
            {"type": "input_text", "text": item["text"]}
            if item["type"] == "text"
            else {"type": "input_image", "image_url": item["image_url"]["url"]}
            for item in content
        ]
        options: dict[str, Any] = {}
        if self.settings.openai_reasoning_effort is not None:
            options["reasoning"] = {"effort": self.settings.openai_reasoning_effort}
        def request() -> tuple[Any, str]:
            response = self._get_client().responses.create(
                model=self.settings.openai_model,
                instructions=system,
                input=[{"role": "user", "content": blocks}],
                max_output_tokens=self.settings.openai_max_output_tokens,
                store=False,
                safety_identifier=sha256(b"health-agent-pilot-v1:" + profile_id.bytes).hexdigest(),
                **options,
            )
            return response, _completed_text(response)
        return _bounded_call(request)

    def transcribe(self, data: bytes) -> str:
        try:
            response = self._get_client().audio.transcriptions.create(
                model=self.settings.openai_transcription_model,
                file=("voice.ogg", data, "audio/ogg"),
                extra_body={"languages": ["ru"]},
            )
        except Exception:  # noqa: BLE001 -- SDK/key details must remain private
            raise ValueError("pilot_voice_unavailable") from None
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
            raise ValueError("pilot_voice_unavailable")
        return text.strip()


def _completed_text(response: Any) -> str:
    if getattr(response, "status", None) == "failed":
        code = getattr(getattr(response, "error", None), "code", None)
        transient = code in {"server_error", "rate_limit_exceeded"}
        raise PilotProviderError("pilot_response_failed", retry_delay=0.5 if transient else None)
    if getattr(response, "status", None) != "completed":
        raise PilotProviderError("pilot_response_incomplete")
    output = getattr(response, "output", None)
    if not isinstance(output, (list, tuple)) or not output:
        raise PilotProviderError("pilot_response_invalid")
    segments: list[str] = []
    for item in output:
        if getattr(item, "type", None) == "reasoning":
            continue
        if getattr(item, "type", None) != "message" or getattr(item, "role", None) != "assistant":
            raise PilotProviderError("pilot_response_invalid")
        if getattr(item, "status", None) != "completed":
            raise PilotProviderError("pilot_response_incomplete")
        contents = getattr(item, "content", None)
        if not isinstance(contents, (list, tuple)) or not contents:
            raise PilotProviderError("pilot_response_invalid")
        for part in contents:
            if getattr(part, "type", None) == "refusal":
                raise PilotProviderError("pilot_response_refused")
            if getattr(part, "type", None) != "output_text" or not isinstance(getattr(part, "text", None), str):
                raise PilotProviderError("pilot_response_invalid")
            segments.append(part.text)
    text = "".join(segments).strip()
    if not text or len(text) > 80_000:
        raise PilotProviderError("pilot_response_invalid")
    return text
