"""Deterministic question evidence selection shared by prompt and presentation."""

from __future__ import annotations

from dataclasses import dataclass, replace

from health_agent.insights.models import HealthSignal
from health_agent.questions.models import (
    EvidenceItem,
    HealthQuestionContext,
    SourceReport,
)

MAX_PRESENTED_EVIDENCE = 60
MAX_PRESENTED_SIGNALS = 30
MAX_ATTENTION_SIGNALS = 5
MAX_CITATION_LABEL_CHARACTERS = 32


@dataclass(frozen=True, slots=True)
class PresentedSignal:
    """One selected aggregate and its single public citation label."""

    signal: HealthSignal
    citation_label: str


@dataclass(frozen=True, slots=True)
class QuestionPresentation:
    """The exact evidence surface available to the responder and the user."""

    evidence: tuple[EvidenceItem, ...]
    signals: tuple[PresentedSignal, ...]
    reports: tuple[SourceReport, ...] = ()

    @property
    def allowed_citations(self) -> frozenset[str]:
        return frozenset(
            [item.citation_label for item in self.evidence]
            + [item.citation_label for item in self.signals]
            + [item.citation_label for item in self.reports]
        )


def select_presentation(context: HealthQuestionContext) -> QuestionPresentation:
    """Select priorities, gaps, then other signals without changing their status."""

    snapshot = context.snapshot
    if snapshot is None:
        selected: tuple[HealthSignal, ...] = ()
    else:
        ordered: list[HealthSignal] = []
        seen: set[HealthSignal] = set()

        def add(signals: tuple[HealthSignal, ...], limit: int | None = None) -> None:
            for signal in signals[:limit]:
                if (
                    signal.citations
                    and signal not in seen
                    and len(ordered) < MAX_PRESENTED_SIGNALS
                ):
                    ordered.append(signal)
                    seen.add(signal)

        add(snapshot.attention, MAX_ATTENTION_SIGNALS)
        add(snapshot.gaps)
        add(snapshot.signals)
        selected = tuple(ordered)

    return QuestionPresentation(
        evidence=tuple(
            replace(
                item,
                citation_label=item.citation_label.strip()[
                    :MAX_CITATION_LABEL_CHARACTERS
                ],
            )
            for item in context.evidence[:MAX_PRESENTED_EVIDENCE]
        ),
        signals=tuple(
            PresentedSignal(
                signal,
                signal.citations[0].citation_id.strip()[:MAX_CITATION_LABEL_CHARACTERS],
            )
            for signal in selected
        ),
        reports=context.reports[:10],
    )
