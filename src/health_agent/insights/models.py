"""Immutable, display-ready contracts for the health overview."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from uuid import UUID


class SignalKind(StrEnum):
    LAB = "lab"
    WEARABLE = "wearable"
    WEIGHT = "weight"


class SignalState(StrEnum):
    ATTENTION = "attention"
    STABLE = "stable"
    GAP = "gap"
    OBSERVED = "observed"


@dataclass(frozen=True, slots=True)
class SourceCitation:
    """Non-PHI source pointer; never contains document text."""

    citation_id: str
    source_kind: str
    source_id: str
    observed_on: date | None = None
    page_number: int | None = None


@dataclass(frozen=True, slots=True)
class HealthSignal:
    kind: SignalKind
    state: SignalState
    title: str
    summary: str
    observed_at: datetime
    citations: tuple[SourceCitation, ...]
    value: str | None = None
    unit: str | None = None
    reference: str | None = None
    explanation_key: str | None = None


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    profile_id: UUID
    as_of: datetime
    attention: tuple[HealthSignal, ...]
    stable: tuple[HealthSignal, ...]
    gaps: tuple[HealthSignal, ...]
    signals: tuple[HealthSignal, ...]
