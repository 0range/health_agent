from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.ai.yandex import YandexLabExtractor, YandexResponsesResponder
from health_agent.config import Settings
from health_agent.lab_extraction.types import ExtractionError
from health_agent.questions.service import QuestionResponderError


class RecordingResponses:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def settings(**kwargs):
    return Settings(_env_file=None, yandex_folder_id="synthetic-folder", **kwargs)


def test_yandex_settings_are_separate_and_deny_every_profile_by_default():
    configured = Settings(_env_file=None)
    assert configured.ai_provider == "openai"
    assert configured.yandex_api_key is None
    assert configured.yandex_api_key_file.as_posix() == ".tokens/yandex-api-key"
    assert configured.yandex_folder_id == ""
    assert configured.yandex_model == "qwen3.6-35b-a3b"
    assert configured.yandex_allowed_profile_ids == ()


def test_yandex_requires_explicit_profile_consent_before_injected_calls():
    responses = RecordingResponses()
    client = SimpleNamespace(responses=responses)
    profile_id = UUID(int=1)
    with pytest.raises(ExtractionError, match="cloud_provider_consent_required"):
        YandexLabExtractor(settings(), client=client).extract(
            profile_id, "Glucose 5.1 mmol/L"
        )
    with pytest.raises(QuestionResponderError, match="unavailable"):
        YandexResponsesResponder(settings(), client=client).respond(
            profile_id=profile_id, question="Synthetic?", context=None
        )
    assert responses.calls == []


def test_yandex_lab_call_is_bounded_stateless_and_reuses_validation():
    profile_id = UUID(int=1)
    output = SimpleNamespace(
        status="completed",
        output=[
            SimpleNamespace(
                type="message",
                status="completed",
                content=[
                    SimpleNamespace(
                        type="output_text",
                        text='{"candidates":[{"source_name":"Glucose","source_value":"5.1","source_unit":"mmol/L","source_flag":null,"reference_text":null,"evidence_excerpt":"Glucose 5.1 mmol/L"}]}',
                    )
                ],
            )
        ],
    )
    responses = RecordingResponses(output)
    extractor = YandexLabExtractor(
        settings(yandex_allowed_profile_ids=(profile_id,)),
        client=SimpleNamespace(responses=responses),
    )
    assert len(extractor.extract(profile_id, "Glucose 5.1 mmol/L")) == 1
    call = responses.calls[0]
    assert call["model"] == "gpt://synthetic-folder/qwen3.6-35b-a3b"
    assert call["store"] is False and call["max_output_tokens"] == 2_000
    assert "reasoning" not in call and "tools" not in call
    with pytest.raises(ExtractionError, match="cloud_provider_consent_required"):
        extractor.extract(UUID(int=2), "Glucose 5.1 mmol/L")
    assert len(responses.calls) == 1


def test_yandex_builds_exact_sdk_client_without_reading_openai_key(monkeypatch):
    profile_id = UUID(int=1)
    built = []
    responses = RecordingResponses(
        SimpleNamespace(
            status="completed",
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    content=[
                        SimpleNamespace(type="output_text", text='{"candidates":[]}')
                    ],
                )
            ],
        )
    )

    def fake_openai(**kwargs):
        built.append(kwargs)
        return SimpleNamespace(responses=responses)

    monkeypatch.setattr("health_agent.ai.yandex.OpenAI", fake_openai)
    configured = settings(
        yandex_api_key="yandex-secret",
        openai_api_key="different-openai-secret",
        yandex_allowed_profile_ids=(profile_id,),
    )
    assert YandexLabExtractor(configured).extract(profile_id, "Unknown marker") == ()
    assert built == [
        {
            "api_key": "yandex-secret",
            "base_url": "https://ai.api.cloud.yandex.net/v1",
            "project": "synthetic-folder",
            "timeout": 30.0,
            "max_retries": 0,
            "default_headers": {"x-data-logging-enabled": "false"},
        }
    ]


@pytest.mark.parametrize("field", ["yandex_folder_id", "yandex_model"])
@pytest.mark.parametrize("value", ["", "bad/path", "bad?query", "bad\nvalue"])
def test_yandex_rejects_invalid_resource_components(field, value):
    options = {"yandex_folder_id": "folder", "yandex_model": "model", field: value}
    with pytest.raises(ValueError):
        YandexLabExtractor(
            Settings(
                _env_file=None,
                yandex_allowed_profile_ids=(UUID(int=1),),
                **options,
            ),
            client=SimpleNamespace(responses=RecordingResponses()),
        )
