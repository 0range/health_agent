"""Small, safe contracts for evidence supplied to a question responder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from health_agent.insights.models import HealthSnapshot


class QuestionIntent(StrEnum):
    """The supported retrieval windows for a free-form health question."""

    GENERAL = "general"
    SLEEP_RECOVERY = "sleep_recovery"
    CURRENT_WEIGHT = "current_weight"
    WEIGHT_TREND = "weight_trend"


class EvidenceSource(StrEnum):
    """Normalized sources which are safe to pass to a responder."""

    LAB = "lab"
    SLEEP = "sleep"
    RECOVERY = "recovery"
    CYCLE = "cycle"
    WORKOUT = "workout"
    WEIGHT = "weight"

    @property
    def citation_prefix(self) -> str:
        return self.value.upper()


class EvidenceTimeSemantics(StrEnum):
    """Meaning of an evidence timestamp, so renderers do not overstate it."""

    OBSERVED = "observed"
    SYNC_AS_OF = "sync_as_of"


class ContextLimitationCode(StrEnum):
    """Stable reason codes for evidence a responder must qualify."""

    WEIGHT_TREND_INSUFFICIENT_HISTORY = "weight_trend_insufficient_history"


@dataclass(frozen=True, slots=True)
class ContextLimitation:
    """Machine-readable limitation with safe text suitable for a user response."""

    code: ContextLimitationCode
    message: str
    prevents_requested_inference: bool = False
    # A mixed request may still have an answerable portion. The limitation's
    # code always forbids its specific inference, independently of this flag.
    prevents_entire_answer: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One display-ready fact, deliberately excluding raw provenance payloads."""

    citation_label: str
    source: EvidenceSource
    observed_at: datetime
    metric: str
    value: str
    unit: str | None = None
    time_semantics: EvidenceTimeSemantics = EvidenceTimeSemantics.OBSERVED
    source_value: str | None = None
    source_unit: str | None = None
    source_reference: str | None = None

    @property
    def citation(self) -> str:
        """Compatibility-friendly short name for the deterministic label."""

        return self.citation_label


@dataclass(frozen=True, slots=True)
class HealthQuestionContext:
    """Bounded evidence selected for exactly one health profile."""

    profile_id: UUID
    intent: QuestionIntent
    window_start: datetime
    window_end: datetime
    evidence: tuple[EvidenceItem, ...]
    source_counts: dict[EvidenceSource, int]
    limitations: tuple[ContextLimitation, ...] = ()
    max_items_per_source: int = 10
    snapshot: HealthSnapshot | None = None

    @property
    def citations(self) -> tuple[EvidenceItem, ...]:
        """The items in deterministic citation order."""

        return self.evidence


# These aliases keep the vocabulary concise for adapters without duplicating data.
QuestionContext = HealthQuestionContext
Evidence = EvidenceItem
