"""Small, safe contracts for evidence supplied to a question responder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class QuestionIntent(StrEnum):
    """The supported retrieval windows for a free-form health question."""

    GENERAL = "general"
    SLEEP_RECOVERY = "sleep_recovery"
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


@dataclass(frozen=True, slots=True)
class EvidenceItem:
    """One display-ready fact, deliberately excluding raw provenance payloads."""

    citation_label: str
    source: EvidenceSource
    observed_at: datetime
    metric: str
    value: str
    unit: str | None = None

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

    @property
    def citations(self) -> tuple[EvidenceItem, ...]:
        """The items in deterministic citation order."""

        return self.evidence


# These aliases keep the vocabulary concise for adapters without duplicating data.
QuestionContext = HealthQuestionContext
Evidence = EvidenceItem
