import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.config import Settings
from health_agent.pilot.brain import PilotBrain

PROFILE = UUID(int=1)


class Client:
    def __init__(self):
        self.calls = []
        self.responses = self
        self.audio = SimpleNamespace(transcriptions=self)
        self.response = SimpleNamespace(
            status="completed", output_text="Ответ",
            output=[SimpleNamespace(type="message", role="assistant", status="completed",
                                    content=[SimpleNamespace(type="output_text", text="Ответ")])],
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if "file" in kwargs:
            return SimpleNamespace(text="Овсянка и яблоко")
        return self.response


def settings(**kwargs):
    return Settings(_env_file=None, ai_provider="openai",
                    openai_allowed_profile_ids=(PROFILE,), **kwargs)


@pytest.mark.parametrize("domain", ["food", "sleep", "training"])
def test_all_domains_use_astra_responses_with_provider_specific_consent(domain, monkeypatch):
    monkeypatch.setattr(Settings, "load_yandex_api_key", lambda _: pytest.fail("Yandex called"))
    client = Client()
    brain = PilotBrain(settings(), PROFILE, domain=domain, client=client)
    assert brain("Test", {"message": "Привет"}) == "Ответ"
    call = client.calls[0]
    assert call["model"] == "gpt-6-astra"
    assert call["reasoning"] == {"effort": "low"}
    assert call["max_output_tokens"] == 4000
    assert call["store"] is False
    assert len(call["safety_identifier"]) == 64
    assert "temperature" not in call and "messages" not in call
    assert json.loads(call["input"][0]["content"][0]["text"])["message"] == "Привет"


def test_openai_consent_does_not_reuse_yandex_consent():
    client = Client()
    legacy_only = Settings(_env_file=None, ai_provider="openai", yandex_allowed_profile_ids=(PROFILE,))
    with pytest.raises(ValueError, match="pilot_provider_consent_required"):
        PilotBrain(legacy_only, PROFILE, client=client)("Test", {})
    assert client.calls == []


def test_image_sent_to_astra_with_responses_image_format(tmp_path):
    path = tmp_path / "plate.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    client = Client()
    PilotBrain(settings(), PROFILE, client=client)("Food", {}, image_path=path)
    content = client.calls[0]["input"][0]["content"]
    assert content[1]["type"] == "input_image"
    assert content[1]["image_url"].startswith("data:image/png;base64,")


@pytest.mark.parametrize("fault", ["incomplete", "refusal", "empty", "tool", "partial_message"])
def test_unusable_output_is_rejected(fault):
    client = Client()
    if fault == "incomplete":
        client.response.status = "incomplete"
    elif fault == "refusal":
        client.response.output[0].content = [SimpleNamespace(type="refusal", refusal="No")]
    elif fault == "empty":
        client.response.output[0].content[0].text = " "
    elif fault == "tool":
        client.response.output.append(SimpleNamespace(type="function_call"))
    else:
        client.response.output[0].status = "incomplete"
    with pytest.raises(ValueError, match="pilot_"):
        PilotBrain(settings(), PROFILE, client=client)("Test", {})


def test_metadata_records_actual_provider_and_redacts_failures():
    from test_training import MemoryStore

    store, client = MemoryStore(), Client()
    brain = PilotBrain(settings(), PROFILE, client=client, store=store, domain="food")
    brain("Food", {})
    saved = store.list(PROFILE, "food", "model_run")[0]
    assert saved.payload["provider"] == "openai"
    assert saved.payload["model"] == "gpt-6-astra"
    assert saved.payload["status"] == "completed"

    def failure(**kwargs):
        raise RuntimeError("private provider detail")

    client.create = failure
    with pytest.raises(ValueError, match="pilot_provider_unavailable") as caught:
        brain("Food", {})
    assert "private provider detail" not in str(caught.value)
    records = store.list(PROFILE, "food", "model_run")
    failed = next(r for r in records if r.payload["status"] == "failed")
    assert "private provider detail" not in str(failed.payload)


def test_voice_uses_openai_with_ogg_and_language_hint(tmp_path):
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggSsynthetic")
    client = Client()
    brain = PilotBrain(settings(), PROFILE, client=client)
    assert brain.transcribe(path) == "Овсянка и яблоко"
    call = client.calls[0]
    assert call["model"] == "gpt-transcribe"
    assert call["file"] == ("voice.ogg", b"OggSsynthetic", "audio/ogg")
    assert call["extra_body"] == {"languages": ["ru"]}
    with pytest.raises(ValueError, match="consent"):
        PilotBrain(settings(), UUID(int=2), client=client).transcribe(path)
    path.write_bytes(b"not an ogg")
    with pytest.raises(ValueError, match="pilot_voice_limit"):
        brain.transcribe(path)
    assert len(client.calls) == 1


def test_openai_client_uses_official_endpoint_even_with_shell_override(monkeypatch):
    from pydantic import SecretStr

    from health_agent.ai.openai import _OpenAIAdapter

    captured = []
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other-provider.invalid/v1")
    monkeypatch.setattr(Settings, "load_openai_api_key", lambda _: SecretStr("test-key"))
    monkeypatch.setattr("health_agent.ai.openai.OpenAI", lambda **kw: captured.append(kw))
    _OpenAIAdapter(settings())._get_client()
    assert captured == [{"api_key": "test-key", "base_url": "https://api.openai.com/v1",
                         "timeout": 60.0, "max_retries": 0}]
