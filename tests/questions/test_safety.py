from __future__ import annotations

import pytest

from health_agent.questions.safety import (
    URGENT_RESPONSE,
    has_urgent_red_flag,
    urgent_response,
)


@pytest.mark.parametrize(
    "question",
    (
        "I have chest pain and cannot breathe.",
        "I have trouble breathing and chest tightness.",
        "У меня боль в груди и мне трудно дышать.",
        "Болит грудь.",
        "Мне не хватает воздуха.",
        "I want to kill myself.",
        "I feel like killing myself.",
        "Я хочу умереть.",
        "Кажется, это признаки инсульта.",
        "Что делать, не могу дышать?",
        "What should I do, I can't breathe?",
        "What should I do, I can’t breathe?",
        "Как быть, хочу умереть?",
        "What should we do? He cannot breathe.",
        "Что делать? Он не может дышать.",
    ),
)
def test_urgent_guard_handles_english_and_russian_red_flags(question: str) -> None:
    assert has_urgent_red_flag(question)
    assert urgent_response(question) == URGENT_RESPONSE
    assert "местному номеру экстренной помощи" in URGENT_RESPONSE
    assert "112" not in URGENT_RESPONSE


def test_urgent_guard_does_not_intercept_ordinary_questions() -> None:
    assert not has_urgent_red_flag("How did I sleep last week?")
    assert urgent_response("How did I sleep last week?") is None


@pytest.mark.parametrize(
    "question",
    (
        "My blood pressure is 120/80; is that normal?",
        "I noticed the atmospheric pressure changed today.",
        "My tire pressure is low.",
    ),
)
def test_urgent_guard_requires_chest_context_for_pressure(question: str) -> None:
    assert not has_urgent_red_flag(question)


@pytest.mark.parametrize(
    "question",
    (
        "What causes chest pain?",
        "Is chest tightness a symptom of anxiety?",
        "What causes shortness of breath?",
        "What causes difficulty breathing?",
        "Что вызывает боль в груди?",
        "Какие причины одышки?",
        "What are stroke symptoms?",
    ),
)
def test_urgent_guard_does_not_intercept_generic_information_questions(
    question: str,
) -> None:
    assert not has_urgent_red_flag(question)
    assert urgent_response(question) is None
