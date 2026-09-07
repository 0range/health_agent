from __future__ import annotations

import copy
import json
from uuid import uuid4

import pytest
from test_runtime import SDKClient
from test_sleep import NOW, FakeBrain, MemoryStore

from health_agent.config import Settings
from health_agent.pilot.brain import PilotBrain
from health_agent.pilot.sleep import SleepCoach
from health_agent.pilot.sleep_grounding import focused_evidence
from health_agent.questions.safety import URGENT_RESPONSE


def context():
    def signal(title, date, value):
        return {
            "kind": "lab",
            "title": title,
            "observed_at": date,
            "value": value,
            "unit": "mg/L",
            "citation_ids": [title],
        }

    return {
        "verified_observations": [
            {
                "metric": "Sleep duration",
                "observed_at": "2026-09-06",
                "value": "6",
                "unit": "h",
            }
        ],
        "health_snapshot": {
            "signals": [
                signal("CRP", "2025-01-01", "historical_marker_sentinel"),
                signal("WBC", "2026-09-06", "5"),
                signal("CRP", "2099-01-01", "future"),
                signal("CRP", None, "undated"),
                {**signal("Sleep duration", "2026-09-06", "6"), "kind": "sleep"},
            ]
        },
        "reported_material": [{"text": "knee insurance sentinel"}],
    }


def test_actual_coach_filters_evidence_and_assistant_history_without_mutation():
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    health = context()
    before = copy.deepcopy(health)
    store.put(
        profile,
        "sleep",
        "turn",
        "old-assistant",
        {"role": "assistant", "text": "unsupported_assistant_claim_sentinel"},
        at=NOW,
    )
    coach = SleepCoach(store, brain, health_context=lambda _p, _q: health)
    coach.handle(
        profile,
        "Почему я в последнее время спать хочу, инфекция или погода?",
        source_key="question",
        now=NOW,
    )
    encoded = json.dumps(brain.calls[-1][1], ensure_ascii=False)
    for forbidden in (
        "historical_marker_sentinel",
        "2099-01-01",
        "undated",
        "unsupported_assistant_claim_sentinel",
        "knee insurance sentinel",
    ):
        assert forbidden not in encoded
    assert "2026-09-06" in encoded
    assert "Sleep duration" in encoded
    assert health == before
    assert any(
        r.payload.get("text") == "unsupported_assistant_claim_sentinel"
        for r in store.list(profile, "sleep", "turn")
    )


@pytest.mark.parametrize(
    "raw",
    [
        "это не инфекция",
        '{"fact_id":"unknown"}',
        '{"fact_id":"fact-0","conclusion":"это не инфекция"}',
    ],
)
def test_causal_output_is_application_rendered(raw):
    brain = FakeBrain(raw)
    reply = SleepCoach(
        MemoryStore(), brain, health_context=lambda _p, _q: context()
    ).handle(uuid4(), "Почему я спать хочу?", source_key="q", now=NOW)
    assert "это не инфекция" not in reply.lower()
    assert "нельзя определить" in reply.lower()
    assert len(reply) <= 1200
    assert reply.count("?") == 1


def test_followup_uses_user_context_and_preserves_denial_and_own_dates():
    store, brain, profile = MemoryStore(), FakeBrain(), uuid4()
    questions = []

    def health(_p, question):
        questions.append(question)
        return context()

    coach = SleepCoach(store, brain, health_context=health)
    coach.handle(profile, "Почему я спать хочу?", source_key="q", now=NOW)
    reply = coach.handle(
        profile, "есть новые анализы, температуры нет", source_key="f", now=NOW
    )
    payload = brain.calls[-1][1]
    encoded = json.dumps(payload, ensure_ascii=False)
    assert "Почему я спать хочу?" in questions[-1]
    assert payload["request"] == "есть новые анализы, температуры нет"
    assert "температуры нет" in encoded
    assert "WBC" in encoded and "2026-09-06" in encoded
    assert "historical_marker_sentinel" not in encoded
    assert "температур" not in reply


def test_pure_sleep_omits_labs_but_historical_question_can_access_them():
    brain = FakeBrain()
    coach = SleepCoach(MemoryStore(), brain, health_context=lambda _p, _q: context())
    coach.handle(uuid4(), "Как улучшить сон?", source_key="s", now=NOW)
    assert "WBC" not in json.dumps(brain.calls[-1][1])
    coach.handle(
        uuid4(), "Как связаны сон и старые анализы CRP?", source_key="h", now=NOW
    )
    encoded = json.dumps(brain.calls[-1][1])
    assert "historical_marker_sentinel" in encoded
    assert "historical" in encoded


def test_causal_boundary_keeps_emergency_guard_local():
    brain = FakeBrain()
    reply = SleepCoach(MemoryStore(), brain).handle(
        uuid4(), "Почему я сонливый и не могу дышать?", source_key="urgent", now=NOW
    )
    assert reply == URGENT_RESPONSE
    assert brain.calls == []


def test_selected_fact_uses_its_own_date_and_no_generated_prose():
    brain = FakeBrain('{"fact_id":"fact-0"}')
    reply = SleepCoach(
        MemoryStore(), brain, health_context=lambda _p, _q: context()
    ).handle(
        uuid4(), "Почему я спать хочу, есть новые анализы?", source_key="q", now=NOW
    )
    assert "Sleep duration — 6" in reply
    assert "2026-09-06" in reply
    assert "2025" not in reply


def test_general_health_question_keeps_original_evidence_and_natural_reply():
    health = context()
    brain = FakeBrain("Обсудим колено.")
    reply = SleepCoach(
        MemoryStore(), brain, health_context=lambda _p, _q: health
    ).handle(uuid4(), "Что написано в отчете про колено?", source_key="q", now=NOW)
    assert reply == "Обсудим колено."
    assert brain.calls[-1][1]["verified_health_context"] == health


def test_final_provider_payload_omits_unrelated_profile_data_only_when_focused():
    profile, store, client = uuid4(), MemoryStore(), SDKClient()
    store.put(
        profile,
        "shared",
        "weight",
        "weight",
        {"source": "apple_health", "weight_kg": 70},
        at=NOW,
    )
    brain = PilotBrain(
        Settings(yandex_folder_id="test", yandex_allowed_profile_ids=(profile,)),
        profile,
        client=client,
        store=store,
        domain="sleep",
    )
    coach = SleepCoach(store, brain, health_context=lambda _p, _q: context())
    coach.handle(profile, "Почему я спать хочу?", source_key="sleep", now=NOW)
    actual = json.loads(client.calls[-1]["messages"][1]["content"][0]["text"])
    assert "apple_weight_history" not in actual
    assert "recorded_food_history" not in actual
    assert "selected_food_framework" not in actual
    assert "shared_goals_not_evidence" in actual
    coach.handle(profile, "Что известно о моем весе?", source_key="weight", now=NOW)
    actual = json.loads(client.calls[-1]["messages"][1]["content"][0]["text"])
    assert actual["apple_weight_history"][0]["weight_kg"] == 70


@pytest.mark.parametrize(
    "question",
    [
        "Почему я хочу спать? Прошлогодние анализы нерелевантны, инфекция сейчас возможна?",
        "Почему я хочу спать с прошлой недели, есть анализы?",
        "Предыдущий вопрос пользователя: Как связаны сон и старые анализы CRP?\nТекущий вопрос: А сейчас инфекция возможна?",
    ],
)
def test_historical_mention_does_not_authorize_old_evidence(question):
    assert "historical_marker_sentinel" not in json.dumps(
        focused_evidence(context(), question, NOW)
    )


@pytest.mark.parametrize(
    "last", ["А это инфекция?", "Началось три дня назад, спал 8 часов"]
)
def test_multiturn_causal_continuity(last):
    brain, store, profile = FakeBrain("это не инфекция"), MemoryStore(), uuid4()
    coach = SleepCoach(store, brain, health_context=lambda _p, _q: context())
    for index, question in enumerate(
        ["Почему я спать хочу?", "есть новые анализы, температуры нет", last]
    ):
        reply = coach.handle(profile, question, source_key=str(index), now=NOW)
    assert brain.calls[-1][1]["causal_reply"] is True
    assert "это не инфекция" not in reply
    if "8 часов" in last:
        assert "Когда началась" not in reply
        assert "сколько часов" not in reply


@pytest.mark.parametrize(
    "question",
    [
        "Что означает анализ холестерина?",
        "Объясни анализ холестерина",
        "Расскажи про холестерин",
    ],
)
def test_new_topic_wins_over_previous_sleep_question(question):
    brain, profile = FakeBrain("Ответ о холестерине"), uuid4()
    coach = SleepCoach(MemoryStore(), brain)
    coach.handle(profile, "Почему я спать хочу?", source_key="s", now=NOW)
    reply = coach.handle(profile, question, source_key="c", now=NOW)
    assert reply == "Ответ о холестерине"
    assert brain.calls[-1][1]["focused_sleep"] is False


def test_production_lab_identity_and_aggregate_not_redated_as_measurement():
    health = {
        "verified_observations": [
            {
                "metric": "white_blood_cells",
                "observed_at": "2026-09-06",
                "value": "5",
                "unit": "10^9/L",
            },
            {
                "metric": "sleep_duration_hours",
                "observed_at": "2026-09-05T06:00:00+00:00",
                "value": "7",
                "unit": "ч",
            },
        ],
        "health_snapshot": {
            "signals": [
                {
                    "kind": "wearable_trend",
                    "title": "Продолжительность сна",
                    "observed_at": NOW.isoformat(),
                    "value": "6.5",
                    "unit": "ч",
                    "summary": "Среднее за 7 полных дней: 6.5; за 28: 7.1",
                },
            ]
        },
    }
    facts = focused_evidence(health, "Почему хочу спать, есть новые анализы?", NOW)[
        "facts"
    ]
    assert facts[0]["metric"] == "white_blood_cells"
    assert facts[0]["observed_at"] == "2026-09-06"
    assert facts[0]["unit"] == "10^9/L"
    assert all(f["value"] != "6.5" for f in facts)
    assert facts[1]["observed_at"] == "2026-09-05T06:00:00+00:00"


@pytest.mark.parametrize(
    "followup",
    [
        "Ну анализы год назад точно нерелевантны, но есть же новые. И нет, темрературы и тд нету",
        "А сейчас инфекция возможна?",
        "Может, дело всё-таки в погоде?",
        "Кстати, температуры нет, новые анализы есть",
        "А это инфекция? Объясни.",
        "Расскажи, может дело в погоде?",
        "Объясни подробнее, почему так устал",
    ],
)
@pytest.mark.parametrize("intermediate", [False, True])
def test_real_question_and_relevant_unanchored_followups_stay_causal(
    followup, intermediate
):
    brain, profile = FakeBrain("это не инфекция"), uuid4()
    coach = SleepCoach(MemoryStore(), brain, health_context=lambda _p, _q: context())
    coach.handle(
        profile,
        "А почему вот я себя чувствую уставшим последние несколько дней и спать хочу сильнее обычного? Это погода влияет? Или может я инфекцию какую-то так переношу?",
        source_key="original",
        now=NOW,
    )
    if intermediate:
        coach.handle(
            profile, "есть новые анализы, температуры нет", source_key="second", now=NOW
        )
    reply = coach.handle(profile, followup, source_key="third", now=NOW)
    assert brain.calls[-1][1]["causal_reply"] is True
    assert "это не инфекция" not in reply
    assert "historical_marker_sentinel" not in json.dumps(brain.calls[-1][1])
