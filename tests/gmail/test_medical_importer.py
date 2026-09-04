from __future__ import annotations

import hashlib
from pathlib import Path

import pymupdf
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

    assert receipt.outcome == "needs_attention"
    assert receipt.processing_status == "image_ocr_required"
    assert receipt.storage_reference is not None
    assert str(DEFAULT_PROFILE_ID) in Path(receipt.storage_reference).parts
