from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pymupdf
import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from health_agent.importer import (
    InvalidReviewTransition,
    approve_observation,
    correct_observation,
    import_document,
    reject_observation,
)
from health_agent.metabase import LAB_HISTORY_QUERY
from health_agent.models import (
    Document,
    DocumentSourceRecord,
    LabObservation,
    Profile,
    ReviewStatus,
    SourceRecord,
)
from health_agent.vault import FileVault


@pytest.fixture
def vault(tmp_path: Path) -> FileVault:
    return FileVault(tmp_path / "vault")


@pytest.fixture
def synthetic_lab_pdf(tmp_path: Path) -> Path:
    path = tmp_path / "labs.pdf"
    document = pymupdf.open()
    page = document.new_page()
    page.insert_text((72, 72), "Ferritin 42 ng/mL 30-400")
    document.save(path)
    document.close()
    return path


def test_reimport_is_duplicate(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    first = import_document(session, vault, synthetic_lab_pdf, "local:test")
    second = import_document(session, vault, synthetic_lab_pdf, "local:test")

    assert first.status == "imported"
    assert second.status == "duplicate"
    assert second.document_id == first.document_id
    assert first.candidate_count == 1
    assert first.review_count == 1


def test_image_import_preserves_original_and_cross_source_dedupe(
    session: Session, vault: FileVault, tmp_path: Path, monkeypatch
) -> None:
    image = tmp_path / "photo.png"
    with pymupdf.open() as pdf:
        pdf.new_page(width=300, height=100).get_pixmap().save(image)
    monkeypatch.setattr(
        "health_agent.images.recognize_image",
        lambda _: "Collection date: 2026-09-05\nFerritin 42 ng/mL 30-400",
    )
    first = import_document(
        session,
        vault,
        image,
        None,
        source_provider="telegram",
        source_external_id="telegram:1:1:1:p",
    )

    def repeated_ocr(_path):
        raise AssertionError("duplicate image must not be re-OCRed")

    monkeypatch.setattr("health_agent.images.recognize_image", repeated_ocr)
    second = import_document(
        session,
        vault,
        image,
        "drive:synthetic",
        source_provider="google_drive",
        source_external_id="same-image",
    )
    document = session.get_one(Document, first.document_id)
    assert document.media_type == "image/png"
    assert Path(document.vault_path).read_bytes() == image.read_bytes()
    assert first.status == "imported" and second.status == "duplicate"
    assert second.document_id == first.document_id
    assert document.collected_date == date(2026, 9, 5)
    assert document.observations[0].status is ReviewStatus.NEEDS_REVIEW
    assert document.pages[0].extraction_method == "local_ocr"
    assert len(document.source_links) == 2


def test_invalid_image_is_not_persisted(
    session: Session, vault: FileVault, tmp_path: Path
):
    path = tmp_path / "broken.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\ninvalid")
    with pytest.raises(ValueError):
        import_document(session, vault, path, None)
    assert not vault.root.exists()


def test_approval_moves_value_into_verified_view(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    report = import_document(session, vault, synthetic_lab_pdf, "local:test")
    observation = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )

    assert observation is not None
    approve_observation(session, observation)
    rows = session.execute(text("SELECT * FROM verified_lab_history")).all()

    assert [
        (
            row.canonical_name,
            row.source_value,
            row.parsed_value,
            row.normalized_value,
            row.normalized_unit,
        )
        for row in rows
    ] == [("ferritin", "42", Decimal(42), Decimal(42), "ng/mL")]
    document = session.get_one(Document, report.document_id)
    assert document.processing_status == "processed"

    duplicate = import_document(session, vault, synthetic_lab_pdf, "local:test")
    assert duplicate.status == "duplicate"
    assert duplicate.processing_status == "processed"


def test_approval_preserves_decimal_comma_and_normalizes_separately(
    session: Session, vault: FileVault, tmp_path: Path
) -> None:
    path = tmp_path / "decimal-comma.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Ferritin 42,5 ng/mL 30-400")
        pdf.save(path)
    report = import_document(
        session, vault, path, "local:comma", collected_date=date(2022, 6, 7)
    )
    observation = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )
    assert observation is not None

    approve_observation(session, observation)
    row = session.execute(
        text(
            "SELECT source_value, parsed_value, normalized_value, normalized_unit "
            "FROM lab_observations WHERE id = :id"
        ),
        {"id": observation},
    ).one()

    assert row == (
        "42,5",
        Decimal("42.5"),
        Decimal("42.5"),
        "ng/mL",
    )


def test_unsupported_pair_cannot_be_approved(
    session: Session, vault: FileVault, tmp_path: Path
) -> None:
    path = tmp_path / "unsupported-pair.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Ferritin 42 mmol/L 30-400")
        pdf.save(path)
    report = import_document(session, vault, path, "local:unsupported")
    observation_id = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )
    assert observation_id is not None

    with pytest.raises(ValueError, match="Unsupported normalization"):
        approve_observation(session, observation_id)

    observation = session.get_one(LabObservation, observation_id)
    assert observation.status is ReviewStatus.NEEDS_REVIEW
    assert observation.normalized_value is None
    assert observation.normalized_unit is None


def test_review_transitions_are_one_way(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    report = import_document(session, vault, synthetic_lab_pdf, "local:test")
    observation_id = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )

    assert observation_id is not None
    reject_observation(session, observation_id)
    assert (
        session.get_one(Document, report.document_id).processing_status == "processed"
    )
    with pytest.raises(InvalidReviewTransition):
        approve_observation(session, observation_id)


def test_correction_preserves_original_and_creates_verified_successor(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    report = import_document(session, vault, synthetic_lab_pdf, "local:test")
    original_id = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )

    assert original_id is not None
    corrected = correct_observation(session, original_id, source_value="43,5")
    original = session.get_one(LabObservation, original_id)

    assert original.source_value == "42"
    assert original.status is ReviewStatus.REJECTED
    assert corrected.status is ReviewStatus.VERIFIED
    assert corrected.source_value == "43,5"
    assert corrected.normalized_value == Decimal("43.5")
    assert corrected.normalized_unit == "ng/mL"
    assert corrected.supersedes_observation_id == original.id
    assert (
        session.get_one(Document, report.document_id).processing_status == "processed"
    )


def test_invalid_correction_leaves_original_pending(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    report = import_document(session, vault, synthetic_lab_pdf, "local:test")
    original_id = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": report.document_id},
    )
    assert original_id is not None

    with pytest.raises(ValueError, match="Unsupported normalization"):
        correct_observation(
            session,
            original_id,
            source_value="43",
            source_unit="mmol/L",
        )

    original = session.get_one(LabObservation, original_id)
    assert original.status is ReviewStatus.NEEDS_REVIEW
    assert original.review_item is not None
    assert original.review_item.decision is None


def test_duplicate_bytes_keep_each_distinct_source_occurrence(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    first = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "local:test",
        source_provider="local_file",
        source_external_id="local-labs.pdf",
    )
    second = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "https://drive.test/file/abc",
        source_provider="google_drive",
        source_external_id="abc",
    )

    assert second.status == "duplicate"
    assert second.document_id == first.document_id
    assert session.query(Document).count() == 1
    assert session.query(SourceRecord).count() == 2
    assert session.query(DocumentSourceRecord).count() == 2
    assert session.query(LabObservation).count() == 1


def test_identical_bytes_are_deduplicated_only_within_one_profile(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    second_profile = Profile(id=uuid4(), name="Second person")
    session.add(second_profile)
    session.flush()

    first = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "local:first",
        collected_date=date(2020, 1, 2),
    )
    second = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "local:second",
        profile_id=second_profile.id,
        collected_date=date(2021, 2, 3),
    )

    assert first.document_id != second.document_id
    documents = session.query(Document).order_by(Document.collected_date).all()
    assert [(row.profile_id, row.collected_date) for row in documents] == [
        (documents[0].profile_id, date(2020, 1, 2)),
        (second_profile.id, date(2021, 2, 3)),
    ]

    first_observation = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": first.document_id},
    )
    assert first_observation is not None
    with pytest.raises(NoResultFound):
        approve_observation(session, first_observation, profile_id=second_profile.id)


def test_chart_excludes_unknown_dates_and_non_default_profiles(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path, tmp_path: Path
) -> None:
    no_date = import_document(session, vault, synthetic_lab_pdf, "local:no-date")
    no_date_observation = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": no_date.document_id},
    )
    assert no_date_observation is not None
    approve_observation(session, no_date_observation)

    second_profile = Profile(id=uuid4(), name="Second person")
    session.add(second_profile)
    session.flush()
    second_path = tmp_path / "second-profile.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Ferritin 44 ng/mL 30-400")
        pdf.save(second_path)
    second = import_document(
        session,
        vault,
        second_path,
        "local:second",
        profile_id=second_profile.id,
        collected_date=date(2020, 1, 1),
    )
    second_observation = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": second.document_id},
    )
    assert second_observation is not None
    approve_observation(session, second_observation, profile_id=second_profile.id)

    assert session.execute(text(LAB_HISTORY_QUERY)).all() == []


def test_scanned_pdf_returns_actionable_ocr_status(
    session: Session, vault: FileVault, tmp_path: Path
) -> None:
    path = tmp_path / "scanned.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page()
        pdf.save(path)

    report = import_document(session, vault, path, "local:scan")
    document = session.get_one(Document, report.document_id)

    assert report.status == "ocr_required"
    assert report.processing_status == "ocr_required"
    assert report.candidate_count == 0
    assert document.processing_status == "ocr_required"
    assert document.safe_error_code == "ocr_required"


def test_conflicting_medical_date_immediately_hides_verified_chart_row(
    session: Session, vault: FileVault, synthetic_lab_pdf: Path
) -> None:
    first = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "local:first",
        collected_date=date(2020, 1, 2),
    )
    observation_id = session.scalar(
        text("SELECT id FROM lab_observations WHERE document_id = :document_id"),
        {"document_id": first.document_id},
    )
    assert observation_id is not None
    approve_observation(session, observation_id)
    assert len(session.execute(text(LAB_HISTORY_QUERY)).all()) == 1

    duplicate = import_document(
        session,
        vault,
        synthetic_lab_pdf,
        "local:conflicting-date",
        collected_date=date(2021, 1, 2),
    )

    assert duplicate.status == "duplicate"
    assert duplicate.processing_status == "needs_attention"
    document = session.get_one(Document, first.document_id)
    assert document.safe_error_code == "conflicting_medical_date"
    assert session.execute(text(LAB_HISTORY_QUERY)).all() == []


def test_lab_like_text_without_candidates_needs_attention(
    session: Session, vault: FileVault, tmp_path: Path
) -> None:
    path = tmp_path / "unsupported-lab.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Glucose 5.1 mmol/L 3.9-5.5")
        pdf.save(path)

    report = import_document(session, vault, path, "local:unsupported-lab")
    document = session.get_one(Document, report.document_id)

    assert report.status == "needs_attention"
    assert report.processing_status == "needs_attention"
    assert document.document_type == "laboratory_report"
    assert document.safe_error_code == "no_lab_candidates"


def test_clear_non_lab_prose_is_not_flagged_as_a_failed_lab_parse(
    session: Session, vault: FileVault, tmp_path: Path
) -> None:
    path = tmp_path / "notes.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), "Meeting notes and a follow-up question.")
        pdf.save(path)

    report = import_document(session, vault, path, "local:notes")
    document = session.get_one(Document, report.document_id)

    assert report.status == "imported"
    assert report.processing_status == "processed"
    assert document.document_type == "unknown_document"
    assert document.safe_error_code is None
