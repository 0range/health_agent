"""Read-only, profile-scoped evidence used by health-question services."""

from health_agent.questions.context import HealthContextBuilder, build_context
from health_agent.questions.models import (
    EvidenceItem,
    EvidenceSource,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.questions.safety import urgent_response

__all__ = (
    "EvidenceItem",
    "EvidenceSource",
    "HealthContextBuilder",
    "HealthQuestionContext",
    "QuestionIntent",
    "build_context",
    "urgent_response",
)
