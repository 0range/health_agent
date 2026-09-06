from __future__ import annotations

import hashlib
import os
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import Engine, event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from health_agent import pdf_evidence
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


def test_descriptor_walk_rejects_ancestor_swapped_to_matching_outside_file(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    document = _legacy_document(session, vault, source)
    prefix = vault.root / document.sha256[:2]
    retained = vault.root / "retained-prefix"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / document.sha256).write_bytes(data)
    real_open = os.open
    swapped = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        if path == document.sha256[:2] and dir_fd is not None and not swapped:
            prefix.rename(retained)
            prefix.symlink_to(outside, target_is_directory=True)
            swapped = True
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(pdf_evidence.os, "open", racing_open)
    with pytest.raises(OSError):
        pdf_evidence._read_vault_pdf(vault, document)
    assert swapped


def test_repair_blocks_outside_hash_and_oversize_sources(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    document = _legacy_document(session, vault, source)

    document.vault_path = str(source)
    session.flush()
    assert repair_pdf_evidence(session, vault, profile_id=DEFAULT_PROFILE_ID).blocked == 1

    document.vault_path = str(vault.root / document.sha256[:2] / document.sha256)
    document.sha256 = "z" * 64
    session.flush()
    assert repair_pdf_evidence(session, vault, profile_id=DEFAULT_PROFILE_ID).blocked == 1

    document.sha256 = hashlib.sha256(data).hexdigest()
    session.flush()
    monkeypatch.setattr(pdf_evidence, "MAX_PDF_BYTES", len(data) - 1)
    assert repair_pdf_evidence(session, vault, profile_id=DEFAULT_PROFILE_ID).blocked == 1


def test_persist_rejects_no_pages_and_page_cap(session: Session, tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    document = _legacy_document(session, vault, source, add_page=False)
    with pytest.raises(ValueError, match="invalid_pdf_evidence"):
        persist_pdf_evidence(
            session, document.id, profile_id=DEFAULT_PROFILE_ID, pdf_bytes=data
        )

    session.add_all(
        DocumentPage(
            document_id=document.id,
            page_number=number,
            extracted_text="synthetic",
            extraction_method="text",
        )
        for number in range(1, 102)
    )
    session.flush()
    with pytest.raises(ValueError, match="invalid_pdf_evidence"):
        persist_pdf_evidence(
            session, document.id, profile_id=DEFAULT_PROFILE_ID, pdf_bytes=data
        )


def test_unsupported_layout_imports_without_evidence(session: Session, tmp_path: Path) -> None:
    source = tmp_path / "narrative.pdf"
    pdf = pymupdf.open()
    page = pdf.new_page()
    page.insert_text((40, 60), "Synthetic narrative without a table")
    pdf.save(source)
    pdf.close()
    report = import_document(session, FileVault(tmp_path / "vault"), source, None)
    assert report.candidate_count == 0
    assert session.scalar(select(func.count(PageEvidence.id))) == 0


def test_competing_persistence_waits_on_document_lock(
    clean_database: Engine, tmp_path: Path
) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    with Session(clean_database) as setup:
        document = _legacy_document(setup, vault, source)
        document_id = document.id
        setup.commit()

    first = Session(clean_database)
    second = Session(clean_database)
    try:
        assert (
            persist_pdf_evidence(
                first,
                document_id,
                profile_id=DEFAULT_PROFILE_ID,
                pdf_bytes=data,
            ).inserted
            == 1
        )
        second.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(DBAPIError):
            persist_pdf_evidence(
                second,
                document_id,
                profile_id=DEFAULT_PROFILE_ID,
                pdf_bytes=data,
            )
    finally:
        second.rollback()
        first.rollback()
        second.close()
        first.close()


def test_capacity_blocks_rows_without_resetting_or_inserting(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    document = _legacy_document(session, vault, source)
    monkeypatch.setattr(pdf_evidence, "MAX_ROWS_PER_PAGE", 0)

    report = persist_pdf_evidence(
        session, document.id, profile_id=DEFAULT_PROFILE_ID, pdf_bytes=data
    )

    assert report.inserted == 0
    assert report.blocked == 1
    assert session.scalar(select(func.count(LabObservation.id))) == 0


def test_failed_evidence_insert_rolls_back_partial_state(
    session: Session, tmp_path: Path
) -> None:
    source = tmp_path / "source.pdf"
    data = _pdf(source)
    vault = FileVault(tmp_path / "vault")
    document = _legacy_document(session, vault, source)

    def fail_evidence_insert(target: Session, *_: object) -> None:
        if any(isinstance(item, PageEvidence) for item in target.new):
            raise RuntimeError("synthetic insert failure")

    event.listen(session, "before_flush", fail_evidence_insert)
    try:
        with pytest.raises(RuntimeError, match="synthetic insert failure"):
            persist_pdf_evidence(
                session,
                document.id,
                profile_id=DEFAULT_PROFILE_ID,
                pdf_bytes=data,
            )
    finally:
        event.remove(session, "before_flush", fail_evidence_insert)
    assert session.scalar(select(func.count(PageEvidence.id))) == 0
    assert session.scalar(select(func.count(LabObservation.id))) == 0


def _legacy_document(
    session: Session, vault: FileVault, source: Path, *, add_page: bool = True
) -> Document:
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
    if add_page:
        session.add(
            DocumentPage(
                document_id=document.id,
                page_number=1,
                extracted_text="column-major synthetic text",
                extraction_method="text",
            )
        )
        session.flush()
    return document
