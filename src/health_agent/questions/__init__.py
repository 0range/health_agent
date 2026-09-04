"""Read-only, profile-scoped evidence used by health-question services."""

from health_agent.questions.context import HealthContextBuilder, build_context
from health_agent.questions.models import (
    ContextLimitation,
    ContextLimitationCode,
    EvidenceItem,
    EvidenceSource,
    EvidenceTimeSemantics,
    HealthQuestionContext,
    QuestionIntent,
)
from health_agent.questions.safety import urgent_response

__all__ = (
    "ContextLimitation",
    "ContextLimitationCode",
    "EvidenceItem",
    "EvidenceSource",
    "EvidenceTimeSemantics",
    "HealthContextBuilder",
    "HealthQuestionContext",
    "QuestionIntent",
    "build_context",
    "urgent_response",
)
