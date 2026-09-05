import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from health_agent.lab_extraction.openai import OpenAILabExtractor
from health_agent.lab_extraction.types import ExtractionError

PROFILE = UUID(int=1)
TEXT = "Glucose\n5.1 mmol/L\n3.9-5.5"
BODY = {
    "candidates": [
        {
            "source_name": "Glucose",
            "source_value": "5.1",
            "source_unit": "mmol/L",
            "source_flag": None,
            "reference_text": "3.9-5.5",
            "evidence_excerpt": TEXT,
        }
    ]
}


class FakeSettings:
    openai_model = "configured-model"
    openai_max_output_tokens = 2000
    openai_reasoning_effort = "low"

    def load_openai_api_key(self):
        pytest.fail("injected client must not read a key")


class Client:
    def __init__(self, *, status="completed", text=None, refusal=False):
        self.responses = self
        self.calls = []
        self.response = SimpleNamespace(
            status=status,
            output_text=json.dumps(BODY) if text is None else text,
            output=[
                SimpleNamespace(
                    type="message",
                    status="completed",
                    content=[
                        SimpleNamespace(type="refusal" if refusal else "output_text")
                    ],
                )
            ],
        )

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def test_responses_contract_is_strict_private_bounded_and_profile_scoped():
    client = Client()
    extractor = OpenAILabExtractor(FakeSettings(), client=client)
    candidates = extractor.extract(PROFILE, TEXT)
    assert candidates[0].canonical_name == "glucose"
    request = client.calls[0]
    assert request["store"] is False
    assert request["model"] == "configured-model"
    assert request["max_output_tokens"] == 2000
    assert request["reasoning"] == {"effort": "low"}
    assert request["text"]["format"]["type"] == "json_schema"
    assert request["text"]["format"]["strict"] is True
    assert request["text"]["format"]["schema"]["additionalProperties"] is False
    assert "tools" not in request
    assert str(PROFILE) not in str(request)
    assert len(request["safety_identifier"]) == 64
    other = Client()
    OpenAILabExtractor(FakeSettings(), client=other).extract(UUID(int=2), TEXT)
    assert other.calls[0]["safety_identifier"] != request["safety_identifier"]


@pytest.mark.parametrize(
    "status", ["incomplete", "failed", "in_progress", "cancelled", None]
)
def test_only_completed_responses_can_supply_candidates(status):
    with pytest.raises(ExtractionError, match="cloud_incomplete"):
        OpenAILabExtractor(FakeSettings(), client=Client(status=status)).extract(
            PROFILE, TEXT
        )


@pytest.mark.parametrize(
    "text",
    [
        "not json private-token",
        "{}",
        "[]",
        json.dumps({"candidates": [], "instructions": "publish verified"}),
    ],
)
def test_invalid_model_output_never_leaks_content(text):
    with pytest.raises(ExtractionError) as error:
        OpenAILabExtractor(FakeSettings(), client=Client(text=text)).extract(
            PROFILE, TEXT
        )
    assert str(error.value) == "cloud_invalid_output"


def test_refusal_and_forged_evidence_are_not_accepted():
    with pytest.raises(ExtractionError, match="cloud_refused"):
        OpenAILabExtractor(FakeSettings(), client=Client(refusal=True)).extract(
            PROFILE, TEXT
        )
    with pytest.raises(ExtractionError, match="cloud_invalid_output"):
        OpenAILabExtractor(FakeSettings(), client=Client()).extract(
            PROFILE, "Unrelated page"
        )


def test_transport_failure_is_one_call_and_safe():
    client = Client()

    def fail(**kwargs):
        client.calls.append(kwargs)
        raise TimeoutError("private-token and medical response")

    client.create = fail
    with pytest.raises(ExtractionError, match="cloud_outcome_unknown"):
        OpenAILabExtractor(FakeSettings(), client=client).extract(PROFILE, TEXT)
    assert len(client.calls) == 1


def test_oversized_page_is_not_sent():
    client = Client()
    with pytest.raises(ExtractionError, match="cloud_input_limit"):
        OpenAILabExtractor(FakeSettings(), client=client).extract(PROFILE, "X" * 12001)
    assert client.calls == []


def test_official_client_uses_shared_key_loader_timeout_and_zero_retries(monkeypatch):
    from pydantic import SecretStr

    calls = []

    class SettingsWithSyntheticKey(FakeSettings):
        def load_openai_api_key(self):
            calls.append("key_loaded")
            return SecretStr("synthetic-test-key")

    def create_client(**kwargs):
        calls.append(kwargs)
        return Client()

    monkeypatch.setattr("health_agent.lab_extraction.openai.OpenAI", create_client)
    extractor = OpenAILabExtractor(SettingsWithSyntheticKey())
    assert calls == []
    extractor.extract(PROFILE, TEXT)
    assert calls == [
        "key_loaded",
        {"api_key": "synthetic-test-key", "timeout": 30.0, "max_retries": 0},
    ]


def test_key_loader_failure_never_exposes_path_or_secret():
    class BrokenSettings(FakeSettings):
        def load_openai_api_key(self):
            raise OSError("private key path and secret")

    with pytest.raises(ExtractionError) as error:
        OpenAILabExtractor(BrokenSettings()).extract(PROFILE, TEXT)
    assert str(error.value) == "openai_not_configured"
