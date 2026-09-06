"""Conservative medical-date inference and bounded archive recovery."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from health_agent.models import Document, DocumentPage

DateRole = Literal["collected", "issued"]


@dataclass(frozen=True, slots=True)
class DateEvidence:
    role: DateRole
    value: date
    page_number: int
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class MedicalDates:
    collected_date: date | None
    issued_date: date | None
    evidence: tuple[DateEvidence, ...]
    blocked_roles: frozenset[str]


_LABELS: dict[DateRole, str] = {
    "collected": (
        r"(?:дата[ \t]+(?:забора|взятия)(?:[ \t]+(?:биоматериала|материала|образца))?"
        r"|(?:collection|collected|specimen)[ \t]+date)"
    ),
    "issued": (
        r"(?:дата[ \t]+выдачи(?:[ \t]+(?:результата|заключения))?"
        r"|дата[ \t]+заключения|(?:issue|issued|report)[ \t]+date)"
    ),
}
_DATE = r"(?:\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4}|\d{1,2}/\d{1,2}/\d{4})"
_SAME_LINE = {
    role: re.compile(
        rf"(?<!\w){label}(?!\w)(?:[ \t]+(?:[:-][ \t]*)?|[ \t]*[:-][ \t]*)"
        rf"(?P<date>{_DATE})(?![\w./-])",
        re.IGNORECASE,
    )
    for role, label in _LABELS.items()
}
_LABEL_LINE = {
    role: re.compile(rf"^[ \t]*{label}[ \t]*$", re.IGNORECASE)
    for role, label in _LABELS.items()
}
_DATE_LINE = re.compile(
    rf"^[ \t]*(?P<date>{_DATE})(?![\w./-])"
    r"(?:[ \t]+(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?)?[ \t]*$"
)


def _parse_date(raw: str, today: date) -> date | None:
    try:
        if "-" in raw:
            value = date.fromisoformat(raw)
        else:
            separator = "." if "." in raw else "/"
            day, month, year = (int(part) for part in raw.split(separator))
            value = date(year, month, day)
    except ValueError:
        return None
    return value if value <= today else None


def _page_evidence(page_number: int, text: str, today: date) -> list[DateEvidence]:
    evidence: list[DateEvidence] = []
    occupied: set[tuple[DateRole, int, int]] = set()
    for role, pattern in _SAME_LINE.items():
        for match in pattern.finditer(text):
            start, end = match.span("date")
            value = _parse_date(match.group("date"), today)
            if value is not None:
                evidence.append(DateEvidence(role, value, page_number, start, end))
                occupied.add((role, start, end))

    lines = text.splitlines(keepends=True)
    offset = 0
    for index, raw_line in enumerate(lines[:-1]):
        line = raw_line.rstrip("\r\n")
        next_raw = lines[index + 1]
        next_line = next_raw.rstrip("\r\n")
        for role, label_pattern in _LABEL_LINE.items():
            if label_pattern.fullmatch(line) is None:
                continue
            date_match = _DATE_LINE.fullmatch(next_line)
            if date_match is None:
                continue
            value = _parse_date(date_match.group("date"), today)
            if value is None:
                continue
            start = offset + len(raw_line) + date_match.start("date")
            end = offset + len(raw_line) + date_match.end("date")
            if (role, start, end) not in occupied:
                evidence.append(DateEvidence(role, value, page_number, start, end))
        offset += len(raw_line)
    return evidence


def infer_medical_dates(
    pages: Iterable[tuple[int, str]], *, today: date | None = None
) -> MedicalDates:
    """Infer only explicitly labelled, unambiguous dates from individual pages."""
    cutoff = today or datetime.now(UTC).date()
    evidence = tuple(
        item
        for page_number, text in pages
        for item in _page_evidence(page_number, str(text), cutoff)
    )
    values = {
        role: {item.value for item in evidence if item.role == role}
        for role in ("collected", "issued")
    }
    blocked = {role for role, found in values.items() if len(found) > 1}
    resolved = {
        role: next(iter(found)) if len(found) == 1 and role not in blocked else None
        for role, found in values.items()
    }
    collected = resolved["collected"]
    issued = resolved["issued"]
    if collected is not None and issued is not None and collected > issued:
        blocked.update(("collected", "issued"))
        collected = issued = None
    return MedicalDates(collected, issued, evidence, frozenset(blocked))


def recover_document_dates(
    session: Session, *, profile_id: UUID, limit: int = 200, apply: bool = False
) -> dict[str, int]:
    """Preview or fill missing dates for a bounded, profile-scoped document set."""
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    ids = session.scalars(
        select(Document.id)
        .where(Document.profile_id == profile_id)
        .where(or_(Document.collected_date.is_(None), Document.issued_date.is_(None)))
        .order_by(Document.id)
        .limit(limit)
    ).all()
    counts = {"scanned": len(ids), "eligible": 0, "changed": 0, "blocked": 0}
    for document_id in ids:
        statement = select(Document).where(
            Document.id == document_id, Document.profile_id == profile_id
        )
        if apply:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        document = session.scalar(statement)
        if document is None:
            continue
        if document.safe_error_code == "conflicting_medical_date":
            counts["blocked"] += 1
            continue
        pages = session.execute(
            select(DocumentPage.page_number, DocumentPage.extracted_text)
            .where(DocumentPage.document_id == document.id)
            .where(DocumentPage.extracted_text.is_not(None))
            .order_by(DocumentPage.page_number)
        ).all()
        found = infer_medical_dates(
            (page_number, text) for page_number, text in pages if text is not None
        )
        proposed_collected = document.collected_date or found.collected_date
        proposed_issued = document.issued_date or found.issued_date
        chronology_blocked = (
            proposed_collected is not None
            and proposed_issued is not None
            and proposed_collected > proposed_issued
        )
        if found.blocked_roles or chronology_blocked:
            counts["blocked"] += 1
        if chronology_blocked:
            continue
        fill_collected = (
            document.collected_date is None and found.collected_date is not None
        )
        fill_issued = document.issued_date is None and found.issued_date is not None
        if not (fill_collected or fill_issued):
            continue
        counts["eligible"] += 1
        if apply:
            if fill_collected:
                document.collected_date = found.collected_date
            if fill_issued:
                document.issued_date = found.issued_date
            counts["changed"] += 1
    return counts
