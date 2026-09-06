"""Read-only, profile-bound retrieval of bounded reported clinical material."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from health_agent.models import Document, DocumentPage
from health_agent.questions.models import SourceReport
from health_agent.visits.models import HealthVisit, HealthVisitNote

MAX_REPORT_TEXT = 1_400
MAX_REPORTS_PER_KIND = 5
MAX_QUALIFYING_PAGES = 60
_ANCHOR = r"(?:Заключение|Рекомендации|Диагноз|Conclusion|Recommendations|Assessment)"
_SECTION_START = re.compile(rf"(?im)^{_ANCHOR}(?:[ \t]*:)?(?:[ \t]+.*)?$")
_SECTION_END = re.compile(rf"(?im)^(?:{_ANCHOR})(?:[ \t]*:)?(?:[ \t]+.*)?$")
_DISQUALIFYING_ERRORS = frozenset(
    {
        "unreadable_original",
        "vault_integrity",
        "unsafe_extraction_path",
        "original_size_limit",
        "original_mime_mismatch",
    }
)
_ANCHOR_PREFIXES = (
    "заключение",
    "рекомендации",
    "диагноз",
    "conclusion",
    "recommendations",
    "assessment",
)


def read_reports(
    session: Session, profile_id: UUID, *, as_of: datetime
) -> tuple[SourceReport, ...]:
    """Return at most five document excerpts and five saved visit answers."""

    boundary = _aware_utc(as_of)
    return (
        *_document_reports(session, profile_id, boundary),
        *_visit_reports(session, profile_id, boundary),
    )


def _document_reports(
    session: Session, profile_id: UUID, as_of: datetime
) -> tuple[SourceReport, ...]:
    statement = (
        select(Document, DocumentPage)
        .join(DocumentPage, DocumentPage.document_id == Document.id)
        .where(
            Document.profile_id == profile_id,
            DocumentPage.extracted_text.is_not(None),
            or_(
                *(
                    DocumentPage.extracted_text.ilike(pattern)
                    for anchor in _ANCHOR_PREFIXES
                    for pattern in (f"{anchor}%", f"%\n{anchor}%")
                )
            ),
        )
        .order_by(Document.created_at.desc(), Document.id, DocumentPage.page_number)
        .limit(MAX_QUALIFYING_PAGES)
    )
    found: list[SourceReport] = []
    seen: set[UUID] = set()
    for document, page in session.execute(statement):
        if len(found) >= MAX_REPORTS_PER_KIND:
            break
        if document.id in seen or document.safe_error_code in _DISQUALIFYING_ERRORS:
            continue
        recorded_at = _aware_utc(document.created_at)
        if recorded_at > as_of:
            continue
        stored_dates = tuple(
            value
            for value in (document.collected_date, document.issued_date)
            if value is not None
        )
        if any(value > as_of.date() for value in stored_dates):
            continue
        medical_date = document.collected_date or document.issued_date
        excerpt = _first_section(page.extracted_text or "")
        if excerpt is None:
            continue
        seen.add(document.id)
        found.append(
            SourceReport(
                f"[DOC{len(found) + 1}]",
                "document_excerpt",
                excerpt,
                f"document:{document.id}#page={page.page_number}",
                medical_date,
                recorded_at,
            )
        )
    return tuple(found)


def _visit_reports(
    session: Session, profile_id: UUID, as_of: datetime
) -> tuple[SourceReport, ...]:
    statement = (
        select(HealthVisitNote, HealthVisit)
        .join(
            HealthVisit,
            (HealthVisit.id == HealthVisitNote.visit_id)
            & (HealthVisit.profile_id == HealthVisitNote.profile_id),
        )
        .where(
            HealthVisitNote.profile_id == profile_id,
            HealthVisitNote.kind == "answer",
            HealthVisit.status != "cancelled",
            HealthVisitNote.created_at <= as_of,
        )
        .order_by(HealthVisitNote.created_at.desc(), HealthVisitNote.id.desc())
        .limit(MAX_REPORTS_PER_KIND)
    )
    reports: list[SourceReport] = []
    for note, visit in session.execute(statement):
        text = _visible_bound(note.text)
        if not text:
            continue
        starts_at = _aware_utc(visit.starts_at)
        reports.append(
            SourceReport(
                f"[VISIT{len(reports) + 1}]",
                "visit_answer",
                text,
                f"visit:{visit.id}#note={note.id}",
                starts_at.date() if starts_at <= as_of else None,
                _aware_utc(note.created_at),
            )
        )
    return tuple(reports)


def _first_section(text: str) -> str | None:
    match = _SECTION_START.search(text)
    if match is None:
        return None
    remainder = text[match.end() :]
    next_section = _SECTION_END.search(remainder)
    blank = re.search(r"\r?\n[ \t]*\r?\n", remainder)
    ends = [item.start() for item in (next_section, blank) if item is not None]
    end = match.end() + (min(ends) if ends else len(remainder))
    excerpt = text[match.start() : end].strip()
    anchor_only = text[match.start() : match.end()].rstrip().endswith(":")
    if not excerpt or (anchor_only and not text[match.end() : end].strip()):
        return None
    # A heading without same-line text still requires body content.
    if "\n" not in excerpt and re.fullmatch(rf"(?i){_ANCHOR}[ \t]*:?", excerpt):
        return None
    return excerpt[:MAX_REPORT_TEXT]


def _visible_bound(text: str) -> str:
    value = text.strip()
    if len(value) <= MAX_REPORT_TEXT:
        return value
    return value[: MAX_REPORT_TEXT - 1].rstrip() + "…"


def _aware_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
