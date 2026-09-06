import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import httpx
import openai
import pytest

from health_agent.ai.yandex import YandexLabExtractor, YandexResponsesResponder
from health_agent.config import Settings
from health_agent.lab_extraction.types import ExtractionError
from health_agent.questions.models import (
    EvidenceItem,
    EvidenceSource,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.questions.service import QuestionResponderError


class RecordingResponses:
    def __init__(self, response=None):
        self.calls = []
        self.response = response

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, BaseException):
            raise self.response
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


def test_authorized_question_is_bounded_stateless_then_denies_second_profile():
    profile_id = UUID(int=1)
    responses = RecordingResponses(
        SimpleNamespace(status="completed", output_text="Synthetic answer. [LAB1]")
    )
    responder = YandexResponsesResponder(
        settings(yandex_allowed_profile_ids=(profile_id,)),
        client=SimpleNamespace(responses=responses),
    )
    assert (
        responder.respond(
            profile_id=profile_id,
            question="What is recorded?",
            context=_question_context(profile_id),
        )
        == "Synthetic answer. [LAB1]"
    )
    call = responses.calls[0]
    assert set(call) == {
        "model",
        "instructions",
        "input",
        "max_output_tokens",
        "store",
        "safety_identifier",
    }
    assert call["model"] == "gpt://synthetic-folder/qwen3.6-35b-a3b"
    assert call["max_output_tokens"] == 2_000
    assert call["store"] is False
    assert "reasoning" not in call and "tools" not in call
    with pytest.raises(QuestionResponderError, match="unavailable"):
        responder.respond(
            profile_id=UUID(int=2),
            question="Must not be sent",
            context=_question_context(UUID(int=2)),
        )
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    ("response", "text", "safe_code"),
    [
        (
            SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        status="completed",
                        content=[
                            SimpleNamespace(
                                type="output_text",
                                text=json.dumps(
                                    {
                                        "candidates": [
                                            {
                                                "source_name": "Glucose",
                                                "source_value": "5.1",
                                                "source_unit": "mmol/L",
                                                "source_flag": None,
                                                "reference_text": None,
                                                "evidence_excerpt": "forged evidence",
                                            }
                                        ]
                                    }
                                ),
                            )
                        ],
                    )
                ],
            ),
            "Glucose 5.1 mmol/L",
            "cloud_invalid_output",
        ),
        (
            SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        status="completed",
                        content=[SimpleNamespace(type="output_text", text="not json")],
                    )
                ],
            ),
            "Glucose 5.1 mmol/L",
            "cloud_invalid_output",
        ),
        (
            SimpleNamespace(
                status="completed",
                output=[
                    SimpleNamespace(
                        type="message",
                        status="completed",
                        content=[SimpleNamespace(type="refusal", text="private")],
                    )
                ],
            ),
            "Glucose 5.1 mmol/L",
            "cloud_refused",
        ),
        (
            SimpleNamespace(status="incomplete", output=[]),
            "Glucose 5.1 mmol/L",
            "cloud_incomplete",
        ),
    ],
)
def test_yandex_lab_rejects_untrusted_response_failures(response, text, safe_code):
    responses = RecordingResponses(response)
    extractor = _authorized_extractor(responses)
    with pytest.raises(ExtractionError, match=safe_code):
        extractor.extract(UUID(int=1), text)
    assert len(responses.calls) == 1


def test_yandex_lab_timeout_is_safe_and_not_retried():
    responses = RecordingResponses(TimeoutError("private provider details"))
    with pytest.raises(ExtractionError, match="cloud_outcome_unknown"):
        _authorized_extractor(responses).extract(UUID(int=1), "Glucose 5.1 mmol/L")
    assert len(responses.calls) == 1


@pytest.mark.parametrize(
    ("exception_type", "status", "body", "safe_code"),
    [
        (openai.AuthenticationError, 401, None, "cloud_auth_required"),
        (openai.RateLimitError, 429, {"code": "other"}, "cloud_rate_limited"),
        (
            openai.RateLimitError,
            429,
            {"code": "insufficient_quota"},
            "cloud_quota_exhausted",
        ),
    ],
)
def test_yandex_lab_sdk_statuses_are_safe_and_not_retried(
    exception_type, status, body, safe_code
):
    response = httpx.Response(
        status, request=httpx.Request("POST", "https://synthetic.invalid/v1/responses")
    )
    responses = RecordingResponses(
        exception_type("private provider details", response=response, body=body)
    )
    with pytest.raises(ExtractionError) as error:
        _authorized_extractor(responses).extract(UUID(int=1), "Glucose 5.1 mmol/L")
    assert str(error.value) == safe_code
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


def _authorized_extractor(responses):
    return YandexLabExtractor(
        settings(yandex_allowed_profile_ids=(UUID(int=1),)),
        client=SimpleNamespace(responses=responses),
    )


def _question_context(profile_id):
    return HealthQuestionContext(
        profile_id=profile_id,
        intent=QuestionIntent.GENERAL,
        window_start=datetime(2026, 9, 1, tzinfo=UTC),
        window_end=datetime(2026, 9, 4, tzinfo=UTC),
        evidence=(
            EvidenceItem(
                citation_label="[LAB1]",
                source=EvidenceSource.LAB,
                observed_at=datetime(2026, 9, 3, tzinfo=UTC),
                metric="Glucose",
                value="5.1",
                unit="mmol/L",
            ),
        ),
        source_counts={EvidenceSource.LAB: 1},
    )
