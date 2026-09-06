from __future__ import annotations

import hashlib
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.importer import correct_observation, import_document
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    LabObservation,
    PageEvidence,
    Profile,
    ReviewStatus,
)
from health_agent.pdf_evidence import persist_pdf_evidence, repair_pdf_evidence
from health_agent.vault import FileVault


def _pdf(path: Path) -> bytes:
    pdf = pymupdf.open()
    page = pdf.new_page(width=600, height=180)
    xs = [30, 190, 280, 390, 480, 570]
    ys = [30, 62, 94]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    values = (
        ("Test", "Glucose"),
        ("Result", "5.10"),
        ("Reference range", "3.9-5.5"),
        ("Unit", "mmol/L"),
        ("Comment", "invented note"),
    )
    for column, column_values in enumerate(values):
        for row, value in enumerate(column_values):
            page.insert_text((xs[column] + 3, ys[row] + 19), value, fontsize=7)
    data = pdf.tobytes()
    pdf.close()
    path.write_bytes(data)
    return data


def test_import_persists_immutable_geometry_and_replays_profile_safely(
    session: Session, tmp_path: Path
) -> None:
    source = tmp_path / "synthetic.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")

    imported = import_document(session, vault, source, None)
    page = session.scalar(
        select(DocumentPage).where(DocumentPage.document_id == imported.document_id)
    )
    assert page is not None
    before = page.extracted_text
    evidence = session.scalar(
        select(PageEvidence).where(PageEvidence.document_id == imported.document_id)
    )
    assert evidence is not None
    rows = tuple(
        session.scalars(
            select(LabObservation).where(
                LabObservation.document_id == imported.document_id,
                LabObservation.page_evidence_id.is_not(None),
            )
        )
    )
    assert page.extracted_text == before
    assert imported.candidate_count == 1
    assert imported.processing_status == "needs_review"
    assert evidence.source_sha256 == hashlib.sha256(data).hexdigest()
    assert evidence.evidence_json["rows"][0]["result"]["text"] == "5.10"
    assert len(evidence.evidence_json["rows"][0]["result"]["bbox"]) == 4
    assert len(rows) == 1
    assert rows[0].status is ReviewStatus.NEEDS_REVIEW
    assert rows[0].page_evidence_id == evidence.id
    assert rows[0].reference_low == Decimal("3.9")
    assert rows[0].reference_high == Decimal("5.5")

    corrected = correct_observation(
        session,
        rows[0].id,
        source_value="5.20",
        profile_id=DEFAULT_PROFILE_ID,
    )
    assert corrected.page_evidence_id == evidence.id
    assert corrected.reference_low == Decimal("3.9")
    assert corrected.reference_high == Decimal("5.5")

    replay = persist_pdf_evidence(
        session, imported.document_id, profile_id=DEFAULT_PROFILE_ID, pdf_bytes=data
    )
    assert replay.inserted == 0
    assert replay.duplicates == 1

    other = Profile(id=uuid4(), name="Other")
    session.add(other)
    session.flush()
    with pytest.raises(ValueError, match="invalid_pdf_evidence"):
        persist_pdf_evidence(
            session, imported.document_id, profile_id=other.id, pdf_bytes=data
        )

    prior = session.scalar(select(func.count(PageEvidence.id)))
    dry = repair_pdf_evidence(
        session, vault, profile_id=DEFAULT_PROFILE_ID, apply=False
    )
    assert dry.inserted == 0
    assert session.scalar(select(func.count(PageEvidence.id))) == prior


def test_repair_dry_run_rolls_back_then_apply_updates_unknown_document(
    session: Session, tmp_path: Path
) -> None:
    source = tmp_path / "legacy.pdf"
    _pdf(source)
    vault = FileVault(tmp_path / "vault")
    stored = vault.store(source)
    document = Document(
        profile_id=DEFAULT_PROFILE_ID,
        sha256=stored.sha256,
        vault_path=str(stored.path),
        media_type="application/pdf",
        document_type="unknown_document",
        issued_date=None,
        collected_date=None,
        processing_status="needs_attention",
        safe_error_code="no_lab_candidates",
    )
    session.add(document)
    session.flush()
    session.add(
        DocumentPage(
            document_id=document.id,
            page_number=1,
            extracted_text="column-major synthetic text",
            extraction_method="text",
        )
    )
    session.flush()

    before = session.scalar(select(func.count(PageEvidence.id)))
    dry = repair_pdf_evidence(
        session, vault, profile_id=DEFAULT_PROFILE_ID, apply=False
    )
    assert dry.supported_pages == 1
    assert dry.inserted == 0
    assert session.scalar(select(func.count(PageEvidence.id))) == before

    applied = repair_pdf_evidence(
        session, vault, profile_id=DEFAULT_PROFILE_ID, apply=True
    )
    assert applied.inserted == 1
    session.refresh(document)
    assert document.document_type == "laboratory_report"
    assert document.processing_status == "needs_review"
    assert document.safe_error_code is None
