"""Bounded OpenAI Platform text, image and voice calls for the pilot coaches."""

from hashlib import sha256
from typing import Any
from uuid import UUID

from openai import OpenAI

from health_agent.config import Settings


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
                raise ValueError("pilot_provider_unavailable") from None
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
        try:
            response = self._get_client().responses.create(
                model=self.settings.openai_model,
                instructions=system,
                input=[{"role": "user", "content": blocks}],
                max_output_tokens=self.settings.openai_max_output_tokens,
                store=False,
                safety_identifier=sha256(b"health-agent-pilot-v1:" + profile_id.bytes).hexdigest(),
                **options,
            )
        except Exception:  # noqa: BLE001 -- SDK/key details must remain private
            raise ValueError("pilot_provider_unavailable") from None
        return response, _completed_text(response)

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
    if getattr(response, "status", None) != "completed":
        raise ValueError("pilot_response_incomplete")
    output = getattr(response, "output", None)
    if not isinstance(output, (list, tuple)) or not output:
        raise ValueError("pilot_response_invalid")
    segments: list[str] = []
    for item in output:
        if getattr(item, "type", None) == "reasoning":
            continue
        if getattr(item, "type", None) != "message" or getattr(item, "role", None) != "assistant":
            raise ValueError("pilot_response_invalid")
        if getattr(item, "status", None) != "completed":
            raise ValueError("pilot_response_incomplete")
        contents = getattr(item, "content", None)
        if not isinstance(contents, (list, tuple)) or not contents:
            raise ValueError("pilot_response_invalid")
        for part in contents:
            if getattr(part, "type", None) == "refusal":
                raise ValueError("pilot_response_refused")
            if getattr(part, "type", None) != "output_text" or not isinstance(getattr(part, "text", None), str):
                raise ValueError("pilot_response_invalid")
            segments.append(part.text)
    text = "".join(segments).strip()
    if not text or len(text) > 80_000:
        raise ValueError("pilot_response_invalid")
    return text
