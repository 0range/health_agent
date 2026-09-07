from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.config import Settings
from health_agent.pilot.brain import PilotBrain
from health_agent.pilot.storage import PilotStore


class Client:
    def __init__(self):
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    finish_reason="stop",
                    message=SimpleNamespace(
                        role="assistant",
                        refusal=None,
                        tool_calls=None,
                        function_call=None,
                        content="Короткий ответ",
                    ),
                )
            ]
        )


def test_consent_and_text_call():
    client = Client()
    settings = Settings(
        yandex_folder_id="test", yandex_allowed_profile_ids=(UUID(int=1),)
    )
    brain = PilotBrain(settings, UUID(int=1), client=client)
    assert brain("Sleep helper", {"message": "Как спал?"}) == "Короткий ответ"
    assert client.calls[0]["store"] is False
    with pytest.raises(ValueError):
        PilotBrain(settings, UUID(int=2), client=client)("Sleep", {})
    assert len(client.calls) == 1


def test_photo_uses_configured_vision_model(tmp_path):
    image = tmp_path / "plate.jpg"
    image.write_bytes(b"\xff\xd8\xffimage")
    client = Client()
    settings = Settings(
        yandex_folder_id="test", yandex_allowed_profile_ids=(UUID(int=1),)
    )
    PilotBrain(settings, UUID(int=1), client=client)("Food", {}, image_path=image)
    call = client.calls[0]
    assert call["model"].endswith(settings.yandex_model)
    assert call["messages"][1]["content"][1]["type"] == "image_url"


def test_model_metadata_durable_and_failure_retains_input(clean_database):
    store = PilotStore(clean_database)
    profile = UUID(int=1)
    client = Client()
    settings = Settings(yandex_folder_id="test", yandex_allowed_profile_ids=(profile,))
    brain = PilotBrain(settings, profile, client=client, store=store, domain="food")
    brain("Food", {"message": "Обед"})
    saved = store.list(profile, "food", "model_run")[0]
    assert saved.payload["status"] == "completed"
    assert saved.payload["output"] == "Короткий ответ"
    assert saved.payload["model"] == client.calls[0]["model"]

    def unavailable(**kwargs):
        raise RuntimeError("private provider error")

    client.create = unavailable
    with pytest.raises(RuntimeError):
        brain("Food", {"message": "Ужин"})
    failed = store.list(profile, "food", "model_run")[0]
    assert failed.payload["input"]["message"] == "Ужин"
    assert failed.payload["error_type"] == "RuntimeError"
    assert "private provider error" not in str(failed.payload)


def test_transcription_uses_ogg_and_consent(monkeypatch, tmp_path):
    import httpx
    from pydantic import SecretStr

    from health_agent.pilot import brain as module

    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"result": "Проснулся бодрым"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(
        Settings, "load_yandex_api_key", lambda _: SecretStr("fake-key")
    )
    path = tmp_path / "voice.ogg"
    path.write_bytes(b"OggSfake")
    settings = Settings(
        yandex_folder_id="test", yandex_allowed_profile_ids=(UUID(int=1),)
    )
    assert PilotBrain(settings, UUID(int=1)).transcribe(path) == "Проснулся бодрым"
    assert calls[0].content == b"OggSfake"
    assert calls[0].headers["x-data-logging-enabled"] == "false"


def test_shared_source_preferences_and_apple_data_do_not_leak_chats(clean_database):
    import json

    store = PilotStore(clean_database)
    profile = UUID(int=1)
    store.put(
        profile,
        "shared",
        "settings",
        "source-priorities",
        {"weight": {"primary": "apple_health"}},
    )
    store.put(
        profile,
        "shared",
        "weight",
        "w1",
        {"source": "apple_health", "weight_kg": 80, "raw_xml": "private original"},
    )
    store.put(
        profile,
        "training",
        "apple_workout",
        "a1",
        {
            "source": "apple_health",
            "upstream_source": "COROS",
            "potential_copy": True,
            "raw_xml": "private original",
        },
    )
    store.put(
        profile,
        "food",
        "turn",
        "privatefood",
        {"text": "food conversation must stay separate"},
    )
    client = Client()
    brain = PilotBrain(
        Settings(yandex_folder_id="test", yandex_allowed_profile_ids=(profile,)),
        profile,
        client=client,
        store=store,
        domain="training",
    )
    brain("Training", {"message": "План недели"})
    payload = json.loads(client.calls[0]["messages"][1]["content"][0]["text"])
    assert payload["source_priorities"]["weight"]["primary"] == "apple_health"
    assert payload["apple_weight_history"][0]["weight_kg"] == 80
    assert payload["apple_workout_candidates"][0]["potential_copy"] is True
    assert "food conversation" not in str(payload)
    assert "private original" not in str(payload)
