from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from health_agent.gmail.medical_importer import MedicalAttachmentImporter
from health_agent.gmail.types import AttachmentProvenance, PreparedAttachment
from health_agent.models import DEFAULT_PROFILE_ID, Document, ReviewItem, SourceRecord
from health_agent.vault import FileVault


def make_pdf(path: Path, text: str) -> PreparedAttachment:
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    content = path.read_bytes()
    return PreparedAttachment(
        path, hashlib.sha256(content).hexdigest(), len(content), "application/pdf"
    )


def make_gridded_pdf(path: Path, *, laboratory: bool) -> PreparedAttachment:
    document = pymupdf.open()
    page = document.new_page(width=600, height=220)
    xs = [30, 190, 280, 390, 480, 570]
    ys = [30, 62, 94]
    for x in xs:
        page.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        page.draw_line((xs[0], y), (xs[-1], y))
    headers = (
        ["Test", "Result", "Reference range", "Unit", "Comment"]
        if laboratory
        else ["Item", "Quantity", "Target", "Measure", "Comment"]
    )
    row = (
        ["Glucose", "5.10", "3.9-5.5", "mmol/L", ""]
        if laboratory
        else ["Widgets", "5", "3-8", "boxes", ""]
    )
    # Column-major insertion reproduces the flat-text ordering that motivated
    # geometry classification without relying on clinical source material.
    values = [headers, row]
    for column in range(5):
        for row_index in range(2):
            value = values[row_index][column]
            if value:
                page.insert_text(
                    (xs[column] + 3, ys[row_index] + 19), value, fontsize=7
                )
    document.save(path)
    document.close()
    content = path.read_bytes()
    return PreparedAttachment(
        path, hashlib.sha256(content).hexdigest(), len(content), "application/pdf"
    )


def provenance(classification: str = "ambiguous") -> AttachmentProvenance:
    return AttachmentProvenance(
        str(DEFAULT_PROFILE_ID),
        "personal",
        "alice@example.com",
        "m1",
        "t1",
        "10",
        1000,
        "1",
        "a1",
        "document.pdf",
        "application/pdf",
        classification,
        "https://mail.google.com/mail/#all/m1",
    )


def test_medical_pdf_enters_common_database_and_review_pipeline(
    clean_database: Engine, session: Session, tmp_path: Path
) -> None:
    prepared = make_pdf(tmp_path / "labs.pdf", "Ferritin 42 ng/mL 30-400")
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    first = importer.import_attachment(provenance(), prepared)
    second = importer.import_attachment(provenance(), prepared)

    assert first.outcome == "medically_imported"
    assert second.outcome == "duplicate"
    assert first.document_id == second.document_id
    assert session.scalars(select(Document)).all()
    assert session.scalars(select(ReviewItem)).all()
    source = session.scalars(select(SourceRecord)).one()
    assert source.provider == "gmail"
    assert source.external_id.startswith("personal:m1:1:m1:1:a1")


def test_generic_nonmedical_pdf_is_classified_without_database_side_effect(
    clean_database: Engine, session: Session, tmp_path: Path
) -> None:
    prepared = make_pdf(tmp_path / "notes.pdf", "Quarterly planning notes")
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    receipt = importer.import_attachment(provenance(), prepared)

    assert receipt.outcome == "non_medical"
    assert session.scalars(select(Document)).all() == []


def test_ambiguous_exact_geometry_lab_enters_pending_review(
    clean_database: Engine, session: Session, tmp_path: Path
) -> None:
    prepared = make_gridded_pdf(tmp_path / "geometry.pdf", laboratory=True)
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    receipt = importer.import_attachment(provenance(), prepared)

    assert receipt.outcome == "medically_imported"
    item = session.scalars(select(ReviewItem)).one()
    assert item.decision is None


def test_ambiguous_ordinary_grid_is_not_classified_as_medical(
    clean_database: Engine, session: Session, tmp_path: Path
) -> None:
    prepared = make_gridded_pdf(tmp_path / "ordinary.pdf", laboratory=False)
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    receipt = importer.import_attachment(provenance(), prepared)

    assert receipt.outcome == "non_medical"
    assert session.scalars(select(Document)).all() == []


def test_geometry_classification_requires_prepared_hash_and_bounded_bytes(
    clean_database: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = make_gridded_pdf(tmp_path / "geometry.pdf", laboratory=True)
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    with pytest.raises(ValueError, match="integrity mismatch"):
        importer.import_attachment(provenance(), replace(prepared, sha256="0" * 64))

    monkeypatch.setattr(
        "health_agent.gmail.medical_importer._MAX_GEOMETRY_PDF_BYTES",
        prepared.size_bytes - 1,
    )
    with pytest.raises(ValueError, match="integrity mismatch"):
        importer.import_attachment(provenance(), prepared)


def test_geometry_classification_observes_page_cap(
    clean_database: Engine,
    session: Session,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "many-pages.pdf"
    document = pymupdf.open()
    for _ in range(101):
        document.new_page().insert_text((72, 72), "Quarterly planning notes")
    document.save(path)
    document.close()
    content = path.read_bytes()
    prepared = PreparedAttachment(
        path,
        hashlib.sha256(content).hexdigest(),
        len(content),
        "application/pdf",
    )
    monkeypatch.setattr(
        "health_agent.gmail.medical_importer.extract_lab_geometry",
        lambda *_args: pytest.fail("geometry detector exceeded its page cap"),
    )
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    receipt = importer.import_attachment(provenance(), prepared)

    assert receipt.outcome == "non_medical"
    assert session.scalars(select(Document)).all() == []


def test_image_is_profile_scoped_and_truthfully_queued_for_ocr(
    clean_database: Engine, tmp_path: Path
) -> None:
    path = tmp_path / "scan.png"
    content = b"\x89PNG\r\n\x1a\nsynthetic"
    path.write_bytes(content)
    prepared = PreparedAttachment(
        path, hashlib.sha256(content).hexdigest(), len(content), "image/png"
    )
    importer = MedicalAttachmentImporter(
        str(DEFAULT_PROFILE_ID),
        "personal",
        clean_database,
        FileVault(tmp_path / "vault"),
    )

    receipt = importer.import_attachment(provenance("suspected_medical"), prepared)

    assert receipt.outcome == "ocr_required"
    assert receipt.processing_status == "image_ocr_required"
    assert receipt.storage_reference is not None
    assert str(DEFAULT_PROFILE_ID) in Path(receipt.storage_reference).parts
