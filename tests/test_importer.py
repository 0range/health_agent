from __future__ import annotations

from pathlib import Path

import pymupdf
import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from health_agent.importer import (
    InvalidReviewTransition,
    approve_observation,
    correct_observation,
    import_document,
    reject_observation,
)
from health_agent.models import LabObservation, ReviewStatus
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

    assert [row.canonical_name for row in rows] == ["ferritin"]


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
    corrected = correct_observation(session, original_id, source_value="43")
    original = session.get_one(LabObservation, original_id)

    assert original.source_value == "42"
    assert original.status is ReviewStatus.REJECTED
    assert corrected.status is ReviewStatus.VERIFIED
    assert corrected.source_value == "43"
    assert corrected.supersedes_observation_id == original.id
