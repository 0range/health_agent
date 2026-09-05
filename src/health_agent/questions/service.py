"""Framework-independent health-question application service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from health_agent.insights.models import HealthSignal
from health_agent.questions.models import (
    ContextLimitation,
    EvidenceItem,
    EvidenceTimeSemantics,
    HealthQuestionContext,
)
from health_agent.questions.safety import guard_urgent_question

QUESTION_UNAVAILABLE_TEXT = (
    "Сейчас не удалось ответить на вопрос о здоровье. Попробуйте ещё раз позже."
)
INSUFFICIENT_EVIDENCE_TEXT = (
    "В выбранном периоде недостаточно проверенных данных о здоровье, чтобы безопасно "
    "ответить на этот вопрос."
)

_BRACKETED_TOKEN = re.compile(r"\[[^\[\]\r\n]*\]")


class QuestionAnswerErrorCode(StrEnum):
    """Closed, public-safe failure codes for the question boundary."""

    INVALID_REQUEST = "invalid_request"
    CONTEXT_UNAVAILABLE = "context_unavailable"
    RESPONDER_UNAVAILABLE = "responder_unavailable"


class QuestionResponderError(RuntimeError):
    """A responder failed without carrying vendor, request, or medical data."""


class HealthQuestionContextBuilder(Protocol):
    """Read-only profile-scoped evidence retrieval boundary."""

    def build(self, profile_id: UUID, question: str) -> HealthQuestionContext: ...


class HealthQuestionResponder(Protocol):
    """Generate text only from an already-bounded health-question context."""

    def respond(
        self,
        *,
        profile_id: UUID,
        question: str,
        context: HealthQuestionContext,
        request_id: str | None = None,
    ) -> str: ...


@dataclass(frozen=True, slots=True)
class QuestionAnswerResult:
    """Safe answer plus structured retrieval metadata for UI adapters."""

    text: str
    safe_error_code: QuestionAnswerErrorCode | None
    evidence: tuple[EvidenceItem, ...] = ()
    limitations: tuple[ContextLimitation, ...] = ()
    urgent: bool = False

    @property
    def available(self) -> bool:
        return self.safe_error_code is None


class HealthQuestionApplicationService:
    """Apply safety, profile-scoped retrieval, and deterministic presentation."""

    def __init__(
        self,
        context_builder: HealthQuestionContextBuilder,
        responder: HealthQuestionResponder,
    ) -> None:
        self._context_builder = context_builder
        self._responder = responder

    def answer(
        self, profile_id: UUID, question: str, *, request_id: str | None = None
    ) -> QuestionAnswerResult:
        """Answer safely; emergency wording never reaches retrieval or a vendor."""

        if not isinstance(question, str) or not question.strip():
            return _unavailable(QuestionAnswerErrorCode.INVALID_REQUEST)

        urgent = guard_urgent_question(question)
        if urgent is not None:
            return QuestionAnswerResult(urgent, None, urgent=True)

        try:
            context = self._context_builder.build(profile_id, question)
        except Exception:  # noqa: BLE001 -- persistence details must not cross this boundary
            return _unavailable(QuestionAnswerErrorCode.CONTEXT_UNAVAILABLE)

        snapshot_has_evidence = bool(
            context.snapshot
            and any(signal.value is not None for signal in context.snapshot.signals)
        )
        if (not context.evidence and not snapshot_has_evidence) or any(
            limitation.prevents_entire_answer for limitation in context.limitations
        ):
            return QuestionAnswerResult(
                text=_with_footer(INSUFFICIENT_EVIDENCE_TEXT, context),
                safe_error_code=None,
                evidence=context.evidence,
                limitations=context.limitations,
            )

        try:
            # CLI requests may omit the delivery identity entirely.
            if request_id is None:
                generated = self._responder.respond(
                    profile_id=profile_id, question=question, context=context
                )
            else:
                generated = self._responder.respond(
                    profile_id=profile_id,
                    question=question,
                    context=context,
                    request_id=request_id,
                )
        except Exception:  # noqa: BLE001 -- never disclose client/provider failure data
            return _unavailable(
                QuestionAnswerErrorCode.RESPONDER_UNAVAILABLE,
                evidence=context.evidence,
                limitations=context.limitations,
            )

        if not isinstance(generated, str) or not generated.strip():
            return _unavailable(
                QuestionAnswerErrorCode.RESPONDER_UNAVAILABLE,
                evidence=context.evidence,
                limitations=context.limitations,
            )
        if not _has_only_valid_citations(generated, context):
            return QuestionAnswerResult(
                text=_with_footer(INSUFFICIENT_EVIDENCE_TEXT, context),
                safe_error_code=None,
                evidence=context.evidence,
                limitations=context.limitations,
            )
        return QuestionAnswerResult(
            text=_with_footer(generated.strip(), context),
            safe_error_code=None,
            evidence=context.evidence,
            limitations=context.limitations,
        )


def render_source_footer(context: HealthQuestionContext) -> str:
    """Render local source provenance in the context's deterministic order."""

    lines = [
        "Источники:",
        (
            f"Выбранный период (включительно, UTC): {context.window_start.isoformat()} — "
            f"{context.window_end.isoformat()}; не более {context.max_items_per_source} "
            "записей из каждого источника."
        ),
        (
            "Для анализов указана календарная дата; для WHOOP — время наблюдения "
            "или синхронизации."
        ),
    ]
    if context.evidence:
        lines.extend(_render_evidence(item) for item in context.evidence)
    else:
        lines.append("- В выбранном периоде нет проверенных данных.")
    if context.snapshot is not None and context.snapshot.signals:
        lines.extend(("", "Снимок здоровья:"))
        lines.extend(
            _render_snapshot_signal(signal) for signal in context.snapshot.signals
        )
    if context.limitations:
        lines.extend(("", "Ограничения:"))
        lines.extend(f"- {limitation.message}" for limitation in context.limitations)
    return "\n".join(lines)


def _render_evidence(item: EvidenceItem) -> str:
    when = item.observed_at.isoformat()
    value = f"{item.value} {item.unit}" if item.unit else item.value
    suffix = (
        " (на момент синхронизации)"
        if item.time_semantics is EvidenceTimeSemantics.SYNC_AS_OF
        else ""
    )
    return f"- {item.citation_label} {when}: {item.metric} — {value}{suffix}"


def _render_snapshot_signal(signal: HealthSignal) -> str:
    """Render the bounded immutable signal without exposing internal source IDs."""

    labels = ", ".join(citation.citation_id for citation in signal.citations)
    value = f"; {signal.value} {signal.unit or ''}".rstrip() if signal.value else ""
    reference = f"; референс источника: {signal.reference}" if signal.reference else ""
    return (
        f"- {labels} {signal.observed_at.isoformat()}: {signal.title} — "
        f"{signal.summary}{value}{reference}"
    )


def _with_footer(answer: str, context: HealthQuestionContext) -> str:
    return f"{answer}\n\n{render_source_footer(context)}"


def _has_only_valid_citations(answer: str, context: HealthQuestionContext) -> bool:
    """Accept data-bearing model text only when it cites local evidence labels.

    All non-empty contexts sent to a responder can support data-dependent claims, so
    requiring a citation for every generated answer is deliberately conservative. It
    avoids trying to infer sentence semantics while making fabricated labels fail closed.
    """

    labels = set(_BRACKETED_TOKEN.findall(answer))
    remainder = _BRACKETED_TOKEN.sub("", answer)
    if "[" in remainder or "]" in remainder:
        return False
    allowed = {item.citation_label for item in context.evidence}
    if context.snapshot is not None:
        allowed.update(
            citation.citation_id
            for signal in context.snapshot.signals
            for citation in signal.citations
        )
    return bool(labels & allowed) and labels <= allowed


def _unavailable(
    code: QuestionAnswerErrorCode,
    *,
    evidence: tuple[EvidenceItem, ...] = (),
    limitations: tuple[ContextLimitation, ...] = (),
) -> QuestionAnswerResult:
    return QuestionAnswerResult(
        QUESTION_UNAVAILABLE_TEXT,
        code,
        evidence=evidence,
        limitations=limitations,
    )
