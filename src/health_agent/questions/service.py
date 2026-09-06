"""Framework-independent health-question application service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from health_agent.questions.models import (
    ContextLimitation,
    EvidenceItem,
    EvidenceSource,
    EvidenceTimeSemantics,
    HealthQuestionContext,
    SourceReport,
)
from health_agent.questions.presentation import PresentedSignal, select_presentation
from health_agent.questions.safety import guard_urgent_question

QUESTION_UNAVAILABLE_TEXT = (
    "Сейчас не удалось ответить на вопрос о здоровье. Попробуйте ещё раз позже."
)
INSUFFICIENT_EVIDENCE_TEXT = (
    "В выбранном периоде недостаточно проверенных данных о здоровье, чтобы безопасно "
    "ответить на этот вопрос."
)

_BRACKETED_TOKEN = re.compile(r"\[[^\[\]\r\n]*\]")
_DOCUMENT_REPORT_REFERENCE = re.compile(
    r"document:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})#page=([1-9][0-9]*)"
)
_VISIT_REPORT_REFERENCE = re.compile(
    r"visit:([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})#note=([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
MAX_RENDERED_REFERENCES = 6
MAX_RENDERED_REFERENCES_WITH_COMPACT_REPORTS = 10
MAX_RENDERED_LIMITATIONS = 3
MAX_RENDERED_METRIC_CHARACTERS = 160
MAX_RENDERED_VALUE_CHARACTERS = 80
MAX_RENDERED_UNIT_CHARACTERS = 32
MAX_RENDERED_REFERENCE_CHARACTERS = 240
MAX_RENDERED_SUMMARY_CHARACTERS = 300


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
        if (
            not context.evidence and not snapshot_has_evidence and not context.reports
        ) or any(
            limitation.prevents_entire_answer for limitation in context.limitations
        ):
            return QuestionAnswerResult(
                text=_with_footer(INSUFFICIENT_EVIDENCE_TEXT, context, set()),
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
                text=_with_footer(INSUFFICIENT_EVIDENCE_TEXT, context, set()),
                safe_error_code=None,
                evidence=context.evidence,
                limitations=context.limitations,
            )
        displayed = _without_citation_labels(generated)
        if not displayed or _contains_internal_source_reference(generated, context):
            return QuestionAnswerResult(
                text=_with_footer(INSUFFICIENT_EVIDENCE_TEXT, context, set()),
                safe_error_code=None,
                evidence=context.evidence,
                limitations=context.limitations,
            )
        return QuestionAnswerResult(
            text=_with_meaningful_limitations(displayed, context),
            safe_error_code=None,
            evidence=context.evidence,
            limitations=context.limitations,
        )


def render_source_footer(
    context: HealthQuestionContext, cited_labels: set[str] | None = None
) -> str:
    """Render only cited facts from the exact prompt selection."""

    cited_labels = cited_labels or set()
    presentation = select_presentation(context)
    report_items = [
        item for item in presentation.reports if item.citation_label in cited_labels
    ]
    report_references = [_render_report(item) for item in report_items]
    other_references = [
        _render_evidence(item)
        for item in presentation.evidence
        if item.citation_label in cited_labels
    ]
    other_references.extend(
        _render_snapshot_signal(item)
        for item in presentation.signals
        if item.citation_label in cited_labels
    )
    references = [*report_references, *other_references]
    lines: list[str] = []
    if references:
        detailed = references[:MAX_RENDERED_REFERENCES]
        detailed_report_count = min(len(report_items), MAX_RENDERED_REFERENCES)
        compact = [
            rendered
            for item in report_items[detailed_report_count:]
            if (rendered := _render_compact_report(item)) is not None
        ][: MAX_RENDERED_REFERENCES_WITH_COMPACT_REPORTS - len(detailed)]
        lines = ["Источники:", *detailed, *compact]
        hidden = len(references) - len(detailed) - len(compact)
        if hidden > 0:
            lines.append(f"- Ещё {hidden} процитированных источников указаны в ответе.")
    if context.limitations:
        if lines:
            lines.append("")
        lines.append("Ограничения:")
        lines.extend(
            f"- {_display_bound(limitation.message, MAX_RENDERED_REFERENCE_CHARACTERS)}"
            for limitation in context.limitations[:MAX_RENDERED_LIMITATIONS]
        )
        hidden = len(context.limitations) - MAX_RENDERED_LIMITATIONS
        if hidden > 0:
            lines.append(f"- Ещё {hidden} ограничений опущены для краткости.")
    return "\n".join(lines)


def _render_evidence(item: EvidenceItem) -> str:
    when = item.observed_at.isoformat()
    display_value = _display_bound(
        item.source_value or item.value, MAX_RENDERED_VALUE_CHARACTERS
    )
    display_unit = item.source_unit if item.source_value is not None else item.unit
    display_unit = (
        _display_bound(display_unit, MAX_RENDERED_UNIT_CHARACTERS)
        if display_unit
        else None
    )
    value = f"{display_value} {display_unit}" if display_unit else display_value
    reference = (
        "; референс источника: "
        f"{_display_bound(item.source_reference or 'unknown', MAX_RENDERED_REFERENCE_CHARACTERS)}"
        if item.source is EvidenceSource.LAB
        else ""
    )
    suffix = (
        " (на момент синхронизации)"
        if item.time_semantics is EvidenceTimeSemantics.SYNC_AS_OF
        else ""
    )
    metric = _display_bound(item.metric, MAX_RENDERED_METRIC_CHARACTERS)
    return f"- {item.citation_label} {when}: {metric} — {value}{reference}{suffix}"


def _render_snapshot_signal(item: PresentedSignal) -> str:
    """Render the bounded immutable signal without exposing internal source IDs."""

    signal = item.signal
    value = (
        "; "
        f"{_display_bound(signal.value, MAX_RENDERED_VALUE_CHARACTERS)} "
        f"{_display_bound(signal.unit or '', MAX_RENDERED_UNIT_CHARACTERS)}".rstrip()
        if signal.value
        else ""
    )
    reference = (
        "; референс источника: "
        f"{_display_bound(signal.reference, MAX_RENDERED_REFERENCE_CHARACTERS)}"
        if signal.reference
        else ""
    )
    title = _display_bound(signal.title, MAX_RENDERED_METRIC_CHARACTERS)
    summary = _display_bound(signal.summary, MAX_RENDERED_SUMMARY_CHARACTERS)
    return (
        f"- {item.citation_label} {signal.observed_at.isoformat()}: {title} — "
        f"{summary}{value}{reference}"
    )


def _render_report(item: SourceReport) -> str:
    kind = (
        "фрагмент документа"
        if item.kind == "document_excerpt"
        else "сохранённая заметка пользователя"
    )
    when = (
        f"медицинская дата {item.medical_date.isoformat()}"
        if item.medical_date is not None
        else f"дата локального архива {item.recorded_at.isoformat()}"
    )
    source_reference = _safe_report_reference(item)
    source = f"; источник {source_reference}" if source_reference is not None else ""
    return (
        f"- {item.citation_label} {kind}; {when}{source}; "
        f"{_display_bound(item.text, 160)}"
    )


def _safe_report_reference(item: SourceReport) -> str | None:
    pattern = (
        _DOCUMENT_REPORT_REFERENCE
        if item.kind == "document_excerpt"
        else _VISIT_REPORT_REFERENCE
        if item.kind == "visit_answer"
        else None
    )
    if pattern is None or pattern.fullmatch(item.source_reference) is None:
        return None
    return item.source_reference


def _render_compact_report(item: SourceReport) -> str | None:
    source_reference = _safe_report_reference(item)
    if source_reference is None:
        return None
    return f"- {item.citation_label} источник {source_reference}"


def _display_bound(value: str, maximum: int) -> str:
    value = " ".join(value.split())
    return value if len(value) <= maximum else f"{value[: maximum - 1]}…"


def _with_footer(
    answer: str, context: HealthQuestionContext, cited_labels: set[str]
) -> str:
    footer = render_source_footer(context, cited_labels)
    return f"{answer}\n\n{footer}" if footer else answer


def _without_citation_labels(answer: str) -> str:
    """Hide already-validated internal labels from the normal user response."""

    lines = []
    for line in _BRACKETED_TOKEN.sub("", answer).splitlines():
        compact = re.sub(r"[ \t]+", " ", line).strip()
        compact = re.sub(r"\s+([,.;:!?])", r"\1", compact)
        lines.append(compact)
    return "\n".join(lines).strip()


def _with_meaningful_limitations(answer: str, context: HealthQuestionContext) -> str:
    limitations = [
        limitation
        for limitation in context.limitations
        if limitation.prevents_requested_inference
    ][:2]
    if not limitations:
        return answer
    detail = " ".join(
        _display_bound(item.message, MAX_RENDERED_REFERENCE_CHARACTERS)
        for item in limitations
    )
    return f"{answer}\n\nВажно: {detail}"


def _contains_internal_source_reference(
    answer: str, context: HealthQuestionContext
) -> bool:
    presentation = select_presentation(context)
    references = {
        item.source_reference
        for item in presentation.evidence
        if item.source_reference and item.source_reference != "unknown"
    } | {
        item.source_reference
        for item in presentation.reports
        if item.source_reference and item.source_reference != "unknown"
    }
    return any(reference in answer for reference in references)


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
    allowed = set(select_presentation(context).allowed_citations)
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
