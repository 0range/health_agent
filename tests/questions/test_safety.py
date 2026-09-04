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
        "У меня боль в груди и мне трудно дышать.",
        "I want to kill myself.",
        "Кажется, это признаки инсульта.",
    ),
)
def test_urgent_guard_handles_english_and_russian_red_flags(question: str) -> None:
    assert has_urgent_red_flag(question)
    assert urgent_response(question) == URGENT_RESPONSE


def test_urgent_guard_does_not_intercept_ordinary_questions() -> None:
    assert not has_urgent_red_flag("How did I sleep last week?")
    assert urgent_response("How did I sleep last week?") is None
