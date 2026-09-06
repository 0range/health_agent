"""Immutable persistence and bounded repair for source-proven PDF table evidence."""

from __future__ import annotations

import hashlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.lab_extraction.types import Candidate
from health_agent.lab_extraction.validation import parse_page_candidates
from health_agent.models import (
    Document,
    DocumentPage,
    LabObservation,
    PageEvidence,
    ReviewItem,
    ReviewStatus,
)
from health_agent.pdf_lab_geometry import (
    GeometryCell,
    GeometryPage,
    extract_lab_geometry,
)
from health_agent.vault import FileVault

MAX_REPAIR_DOCUMENTS = 150
MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_PAGES = 100
MAX_ROWS_PER_PAGE = 40
_PROTECTED_DOCUMENT_ERRORS = frozenset({"vault_integrity", "conflicting_medical_date"})


@dataclass(frozen=True, slots=True)
class EvidencePersistenceReport:
    scanned: int = 0
    supported_pages: int = 0
    inserted: int = 0
    duplicates: int = 0
    blocked: int = 0


def persist_pdf_evidence(
    session: Session, document_id: UUID, *, profile_id: UUID, pdf_bytes: bytes
) -> EvidencePersistenceReport:
    """Persist exact geometry rows for one owned document, replaying idempotently."""

    if not isinstance(pdf_bytes, bytes) or not 0 < len(pdf_bytes) <= MAX_PDF_BYTES:
        raise ValueError("invalid_pdf_evidence")
    document = session.scalar(
        select(Document).where(
            Document.id == document_id, Document.profile_id == profile_id
        )
    )
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    if (
        document is None
        or document.media_type != "application/pdf"
        or document.sha256 != digest
    ):
        raise ValueError("invalid_pdf_evidence")
    page_numbers = tuple(
        session.scalars(
            select(DocumentPage.page_number)
            .where(DocumentPage.document_id == document_id)
            .order_by(DocumentPage.page_number)
            .limit(MAX_PAGES + 1)
        )
    )
    if not page_numbers or len(page_numbers) > MAX_PAGES:
        raise ValueError("invalid_pdf_evidence")
    geometry = tuple(extract_lab_geometry(pdf_bytes, number) for number in page_numbers)

    locked = session.scalar(
        select(Document)
        .where(Document.id == document_id, Document.profile_id == profile_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked is None or locked.sha256 != digest:
        raise ValueError("invalid_pdf_evidence")
    with session.begin_nested():
        return _persist_pages(session, locked, geometry)


def _persist_pages(
    session: Session, document: Document, pages: tuple[GeometryPage, ...]
) -> EvidencePersistenceReport:
    supported = inserted = duplicates = blocked = 0
    existing_rows = tuple(
        session.scalars(
            select(LabObservation).where(LabObservation.document_id == document.id)
        )
    )
    identities = {_identity(row) for row in existing_rows}
    page_counts: dict[int, int] = {}
    for row in existing_rows:
        page_counts[row.page_number] = page_counts.get(row.page_number, 0) + 1
    for page in pages:
        if not page.rows:
            continue
        supported += 1
        content = _page_json(page)
        evidence = session.scalar(
            select(PageEvidence).where(
                PageEvidence.document_id == document.id,
                PageEvidence.page_number == page.page_number,
                PageEvidence.method == page.method,
                PageEvidence.source_sha256 == page.source_sha256,
            )
        )
        if evidence is not None and evidence.evidence_json != content:
            raise ValueError("pdf_evidence_conflict")
        if evidence is None:
            evidence = PageEvidence(
                document_id=document.id,
                page_number=page.page_number,
                method=page.method,
                source_sha256=page.source_sha256,
                evidence_json=content,
            )
            session.add(evidence)
            session.flush()
        candidates = parse_page_candidates(page.text).candidates
        novel = [
            candidate
            for candidate in candidates
            if _candidate_identity(page.page_number, candidate) not in identities
        ]
        duplicates += len(candidates) - len(novel)
        available = MAX_ROWS_PER_PAGE - page_counts.get(page.page_number, 0)
        if len(novel) > available:
            blocked += len(novel)
            continue
        for candidate in novel:
            observation = LabObservation(
                document_id=document.id,
                page_number=page.page_number,
                page_evidence_id=evidence.id,
                canonical_name=candidate.canonical_name,
                source_name=candidate.source_name,
                source_value=candidate.source_value,
                parsed_value=candidate.parsed_value,
                source_unit=candidate.source_unit,
                source_flag=candidate.source_flag,
                normalized_value=None,
                normalized_unit=None,
                reference_low=candidate.reference_low,
                reference_high=candidate.reference_high,
                reference_text=candidate.reference_text,
                evidence_excerpt=candidate.evidence_excerpt,
                confidence=0.5,
                status=ReviewStatus.NEEDS_REVIEW,
            )
            session.add(observation)
            session.flush()
            session.add(
                ReviewItem(observation=observation, reason_code="pdf_table_candidate")
            )
            identities.add(_identity(observation))
            inserted += 1
        page_counts[page.page_number] = page_counts.get(page.page_number, 0) + len(
            novel
        )
    session.flush()
    if inserted and document.safe_error_code not in _PROTECTED_DOCUMENT_ERRORS:
        document.document_type = "laboratory_report"
        document.processing_status = "needs_review"
        if document.safe_error_code == "no_lab_candidates":
            document.safe_error_code = None
    return EvidencePersistenceReport(
        len(pages), supported, inserted, duplicates, blocked
    )


def repair_pdf_evidence(
    session: Session,
    vault: FileVault,
    *,
    profile_id: UUID,
    limit: int = MAX_REPAIR_DOCUMENTS,
    apply: bool = False,
) -> EvidencePersistenceReport:
    """Repair a bounded profile selection; dry runs roll back every candidate write."""

    if not 1 <= limit <= MAX_REPAIR_DOCUMENTS:
        raise ValueError("invalid_pdf_evidence_limit")
    documents = tuple(
        session.scalars(
            select(Document)
            .where(
                Document.profile_id == profile_id,
                Document.media_type == "application/pdf",
            )
            .order_by(Document.created_at, Document.id)
            .limit(limit)
        )
    )
    total = EvidencePersistenceReport()
    for document in documents:
        savepoint = session.begin_nested()
        try:
            data = _read_vault_pdf(vault, document)
            report = persist_pdf_evidence(
                session, document.id, profile_id=profile_id, pdf_bytes=data
            )
            if apply:
                savepoint.commit()
            else:
                savepoint.rollback()
                report = EvidencePersistenceReport(
                    report.scanned,
                    report.supported_pages,
                    0,
                    report.duplicates,
                    report.blocked,
                )
        except (OSError, ValueError):
            if savepoint.is_active:
                savepoint.rollback()
            report = EvidencePersistenceReport(blocked=1)
        total = EvidencePersistenceReport(
            total.scanned + 1,
            total.supported_pages + report.supported_pages,
            total.inserted + report.inserted,
            total.duplicates + report.duplicates,
            total.blocked + report.blocked,
        )
    return total


def _read_vault_pdf(vault: FileVault, document: Document) -> bytes:
    root = vault.root.absolute()
    path = Path(document.vault_path).absolute()
    expected = root / document.sha256[:2] / document.sha256
    if (
        path != expected
        or len(document.sha256) != 64
        or any(character not in "0123456789abcdef" for character in document.sha256)
    ):
        raise ValueError("invalid_pdf_evidence")
    root_descriptor = _open_directory_without_symlinks(root)
    try:
        prefix_descriptor = os.open(
            document.sha256[:2],
            os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_descriptor,
        )
        try:
            descriptor = os.open(
                document.sha256,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=prefix_descriptor,
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_PDF_BYTES:
                    raise ValueError("invalid_pdf_evidence")
                chunks: list[bytes] = []
                remaining = MAX_PDF_BYTES + 1
                while remaining and (
                    chunk := os.read(descriptor, min(1024 * 1024, remaining))
                ):
                    chunks.append(chunk)
                    remaining -= len(chunk)
                data = b"".join(chunks)
            finally:
                os.close(descriptor)
        finally:
            os.close(prefix_descriptor)
    finally:
        os.close(root_descriptor)
    if len(data) > MAX_PDF_BYTES or hashlib.sha256(data).hexdigest() != document.sha256:
        raise ValueError("invalid_pdf_evidence")
    return data


def _open_directory_without_symlinks(path: Path) -> int:
    if not path.is_absolute():
        raise ValueError("invalid_pdf_evidence")
    flags = os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except Exception:
        os.close(descriptor)
        raise
    return descriptor


def _page_json(page: GeometryPage) -> dict[str, object]:
    return {
        "method": page.method,
        "source_sha256": page.source_sha256,
        "rows": [
            {
                "name": _cell_json(row.name),
                "result": _cell_json(row.result),
                "unit": _cell_json(row.unit),
                "reference": _cell_json(row.reference),
                "comment": _cell_json(row.comment) if row.comment is not None else None,
                "derived_line": row.derived_line,
            }
            for row in page.rows
        ],
    }


def _cell_json(cell: GeometryCell) -> dict[str, object]:
    return {"text": cell.text, "bbox": list(cell.bbox)}


def _identity(row: LabObservation) -> tuple[object, ...]:
    return (
        row.page_number,
        row.source_name,
        row.source_value,
        row.source_unit,
        row.reference_text,
        row.source_flag,
    )


def _candidate_identity(page_number: int, candidate: Candidate) -> tuple[object, ...]:
    return (
        page_number,
        candidate.source_name,
        candidate.source_value,
        candidate.source_unit,
        candidate.reference_text,
        candidate.source_flag,
    )
