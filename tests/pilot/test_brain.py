from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.models import Profile
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


@pytest.mark.parametrize("domain", ["sleep", "food", "training"])
def test_shared_context_excludes_inactive_goals_but_keeps_dated_future_stage(domain):
    from test_training import MemoryStore

    store, profile = MemoryStore(), UUID(int=1)
    for status in ["active", "planned", "paused", "done"]:
        store.put(profile, "shared", "goal", status, {
            "title": status, "status": status, "period_start": "2027-01-01",
        })
    brain = PilotBrain(Settings(yandex_folder_id="test"), profile, store=store, domain=domain)
    context = brain._profile_context()
    assert {g["status"] for g in context["shared_goals_not_evidence"]} == {"active", "planned"}
    assert all(g["period_start"] == "2027-01-01" for g in context["shared_goals_not_evidence"])


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


def test_sleep_context_has_own_food_facts_and_selected_framework_only(clean_database):
    import json

    store = PilotStore(clean_database)
    profile, other = UUID(int=1), UUID(int=2)
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Other"))
    now = datetime.now(UTC)
    store.put(profile, "food", "meal", "own", {
        "occurred_at": now.isoformat(), "category": "lunch",
        "photo_path": "/private/photo.jpg", "original": "private own text",
        "analysis": {"foods": ["рис"], "kcal": 300, "feedback": "private model text"},
    })
    store.put(other, "food", "meal", "other", {
        "occurred_at": now.isoformat(), "original": "other profile secret",
    })
    store.put(profile, "food", "settings", "protocol", {
        "interval_hours": 3.5, "allowed_interval_hours": [3, 3.5, 4],
        "plate_rules": {"lunch": ["vegetables", "protein"]},
        "preferences": ["без рыбы"], "uncertainties": ["порции"],
        "private_notes": "must not leak",
    })
    settings = Settings(yandex_folder_id="test", yandex_allowed_profile_ids=(profile,))

    sleep_client = Client()
    PilotBrain(settings, profile, client=sleep_client, store=store, domain="sleep")(
        "Sleep", {"message": "Как спал?"}
    )
    payload = json.loads(sleep_client.calls[0]["messages"][1]["content"][0]["text"])
    history = payload["recorded_food_history"]
    assert history["recorded_meal_count"] == 1
    assert history["meals"][0]["foods"] == ["рис"]
    assert "private" not in str(history) and "other profile secret" not in str(history)
    framework = payload["selected_food_framework"]
    assert framework["interval_hours"] == 3.5
    assert framework["source"] == "user_selected"
    assert framework["not_universal_medical_rules"] is True
    assert "private_notes" not in str(framework)

    training_client = Client()
    PilotBrain(settings, profile, client=training_client, store=store, domain="training")(
        "Training", {"message": "План"}
    )
    training_payload = json.loads(
        training_client.calls[0]["messages"][1]["content"][0]["text"]
    )
    assert "recorded_food_history" not in training_payload
    assert "selected_food_framework" not in training_payload


@pytest.mark.parametrize(
    "invalid",
    [float("nan"), float("inf"), -1, 0, 2.49, 4.51, 10**100, True, "3.5"],
)
def test_selected_food_framework_rejects_unsupported_intervals(invalid):
    from health_agent.pilot.brain import _selected_food_framework

    framework = _selected_food_framework({
        "interval_hours": invalid,
        "allowed_interval_hours": [3, invalid, 4.5],
        "preferences": ["keep user preference"],
    })
    assert framework["interval_hours"] is None
    assert framework["allowed_interval_hours"] == [3, 4.5]
    assert framework["preferences"] == ["keep user preference"]
