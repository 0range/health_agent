from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import UUID

import pymupdf
import pytest
from sqlalchemy import Engine, select

from health_agent.db import session_scope
from health_agent.google_drive.medical_consumer import MedicalDriveConsumer
from health_agent.google_drive.types import DriveItem, DriveProvenance
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentSourceRecord,
    Profile,
    ReviewItem,
    SourceRecord,
)
from health_agent.vault import FileVault

BOB = UUID("22222222-2222-4222-8222-222222222222")


def pdf_bytes(
    tmp_path: Path, text: str = "Ferritin 42 ng/mL 30-400", name: str = "lab.pdf"
) -> bytes:
    path = tmp_path / name
    with pymupdf.open() as document:
        page = document.new_page()
        page.insert_text((72, 72), text)
        document.save(path)
    return path.read_bytes()


def provenance(profile_id: UUID) -> DriveProvenance:
    return DriveProvenance(
        profile_id=str(profile_id),
        root_folder_id="root-folder-123",
        folder_path=("Health",),
        item=DriveItem(
            file_id="same-drive-file",
            name="labs.pdf",
            mime_type="application/pdf",
            parent_ids=("root-folder-123",),
            version="7",
            can_download=True,
            web_view_link="https://drive.google.com/file/d/same-drive-file/view",
        ),
        output_media_type="application/pdf",
        exported_from_google_native=False,
    )


def test_pdf_enters_database_review_and_remains_profile_isolated(
    clean_database: Engine, tmp_path: Path
) -> None:
    with session_scope(clean_database) as session:
        session.add(Profile(id=BOB, name="Bob"))
    content = pdf_bytes(tmp_path)
    vault = FileVault(tmp_path / "vault")

    alice_receipt = MedicalDriveConsumer(
        str(DEFAULT_PROFILE_ID), clean_database, vault, tmp_path / "tmp"
    ).consume(provenance(DEFAULT_PROFILE_ID), iter((content[:50], content[50:])))
    bob_receipt = MedicalDriveConsumer(
        str(BOB), clean_database, vault, tmp_path / "tmp"
    ).consume(provenance(BOB), iter((content,)))

    assert alice_receipt.outcome == "medically_imported"
    assert bob_receipt.outcome == "medically_imported"
    assert alice_receipt.document_id != bob_receipt.document_id
    with session_scope(clean_database) as session:
        documents = session.scalars(select(Document).order_by(Document.profile_id)).all()
        sources = session.scalars(select(SourceRecord)).all()
        assert {document.profile_id for document in documents} == {
            DEFAULT_PROFILE_ID,
            BOB,
        }
        assert {(source.profile_id, source.external_id, source.revision) for source in sources} == {
            (DEFAULT_PROFILE_ID, "same-drive-file", "7|||"),
            (BOB, "same-drive-file", "7|||"),
        }
        assert all(source.provider == "google_drive" for source in sources)
        assert session.query(DocumentSourceRecord).count() == 2
        assert session.query(ReviewItem).count() == 2


def test_missing_database_profile_fails_closed(clean_database: Engine, tmp_path: Path) -> None:
    consumer = MedicalDriveConsumer(
        str(BOB), clean_database, FileVault(tmp_path / "vault"), tmp_path / "tmp"
    )
    with pytest.raises(RuntimeError, match="does not exist"):
        consumer.consume(provenance(BOB), iter((pdf_bytes(tmp_path),)))


def test_explicit_issue_date_is_imported_without_using_drive_timestamps(
    clean_database: Engine, tmp_path: Path
) -> None:
    content = pdf_bytes(
        tmp_path,
        "Report date: 07.05.2024\nFerritin 42 ng/mL 30-400",
        "issued.pdf",
    )
    consumer = MedicalDriveConsumer(
        str(DEFAULT_PROFILE_ID),
        clean_database,
        FileVault(tmp_path / "vault"),
        tmp_path / "tmp",
    )

    receipt = consumer.consume(provenance(DEFAULT_PROFILE_ID), iter((content,)))

    assert receipt.document_id is not None
    with session_scope(clean_database) as session:
        document = session.get_one(Document, UUID(receipt.document_id))
        assert document.collected_date is None
        assert document.issued_date == date(2024, 5, 7)
