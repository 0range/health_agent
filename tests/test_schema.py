from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import insert

from alembic import command
from health_agent.config import Settings
from health_agent.models import (
    DEFAULT_PROFILE_ID,
    Document,
    DocumentPage,
    DocumentSourceRecord,
    LabObservation,
    ReviewStatus,
    SourceRecord,
)


def test_settings_builds_a_local_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    settings = Settings(postgres_password="space password")

    assert settings.database_url == (
        "postgresql+psycopg://health_agent:space%20password@127.0.0.1:55432/health_agent"
    )


def test_review_status_binds_lowercase_values() -> None:
    statement = insert(LabObservation).values(status=ReviewStatus.VERIFIED)

    compiled = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )

    assert "'verified'" in compiled


def make_document(session: Session, identity: str = "1") -> Document:
    source = SourceRecord(
        profile_id=DEFAULT_PROFILE_ID,
        provider="local_file",
        external_id=f"abc-{identity}",
        revision=f"sha256:{identity}",
    )
    document = Document(
        profile_id=DEFAULT_PROFILE_ID,
        sha256=identity * 64,
        vault_path=f"data/vault/{identity}{identity}/" + identity * 64,
        media_type="application/pdf",
        document_type="laboratory_report",
        processing_status="pending",
    )
    session.add_all((source, document))
    session.flush()
    session.add(
        DocumentSourceRecord(
            document_id=document.id,
            source_record_id=source.id,
            profile_id=DEFAULT_PROFILE_ID,
        )
    )
    session.flush()
    return document


def make_page(
    session: Session, document: Document, page_number: int = 1
) -> DocumentPage:
    page = DocumentPage(
        document=document,
        page_number=page_number,
        extracted_text="Ferritin 42 ng/mL",
        extraction_method="digital_text",
    )
    session.add(page)
    session.flush()
    return page


def test_same_source_revision_is_unique(session: Session) -> None:
    session.add(
        SourceRecord(
            profile_id=DEFAULT_PROFILE_ID,
            provider="local_file",
            external_id="abc",
            revision="sha256:1",
        )
    )
    session.flush()
    session.add(
        SourceRecord(
            profile_id=DEFAULT_PROFILE_ID,
            provider="local_file",
            external_id="abc",
            revision="sha256:1",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_review_required_observation_is_not_publishable(session: Session) -> None:
    document = make_document(session)
    make_page(session, document)
    observation = LabObservation(
        document=document,
        page_number=1,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        parsed_value=42,
        source_unit="ng/mL",
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=0.7,
        status=ReviewStatus.NEEDS_REVIEW,
    )

    assert observation.is_publishable is False
    session.add(observation)


def test_verified_history_view_excludes_non_verified_observations(
    session: Session,
) -> None:
    document = make_document(session)
    make_page(session, document)
    verified = LabObservation(
        document=document,
        page_number=1,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        parsed_value=42,
        source_unit="ng/mL",
        normalized_value=42,
        normalized_unit="ng/mL",
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=0.9,
        status=ReviewStatus.VERIFIED,
    )
    pending = LabObservation(
        document=document,
        page_number=1,
        canonical_name="vitamin_d",
        source_name="Vitamin D",
        source_value="30",
        source_unit="ng/mL",
        evidence_excerpt="Vitamin D 30 ng/mL",
        confidence=0.6,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    session.add_all((verified, pending))
    session.flush()

    names = (
        session.execute(
            text(
                "SELECT canonical_name FROM verified_lab_history ORDER BY canonical_name"
            )
        )
        .scalars()
        .all()
    )
    status = session.execute(
        text("SELECT status::text FROM lab_observations WHERE id = :observation_id"),
        {"observation_id": verified.id},
    ).scalar_one()

    assert names == ["ferritin"]
    assert status == "verified"


def test_verified_observation_requires_normalized_value_and_unit(
    session: Session,
) -> None:
    document = make_document(session)
    make_page(session, document)
    session.add(
        LabObservation(
            document=document,
            page_number=1,
            canonical_name="ferritin",
            source_name="Ferritin",
            source_value="42",
            source_unit="ng/mL",
            normalized_value=None,
            normalized_unit=None,
            evidence_excerpt="Ferritin 42 ng/mL",
            confidence=0.9,
            status=ReviewStatus.VERIFIED,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_correction_lineage_must_stay_in_the_same_document(
    session: Session,
) -> None:
    original_document = make_document(session)
    make_page(session, original_document)
    other_document = make_document(session, identity="2")
    make_page(session, other_document)
    original = LabObservation(
        document=original_document,
        page_number=1,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        parsed_value=42,
        source_unit="ng/mL",
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=0.7,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    session.add(original)
    session.flush()
    session.add(
        LabObservation(
            document=other_document,
            page_number=1,
            supersedes_observation_id=original.id,
            canonical_name="ferritin",
            source_name="Ferritin",
            source_value="43",
            parsed_value=43,
            source_unit="ng/mL",
            evidence_excerpt="Ferritin 43 ng/mL",
            confidence=0.7,
            status=ReviewStatus.NEEDS_REVIEW,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_downgrade_refuses_to_erase_non_default_profile(
    clean_database: Engine,
) -> None:
    second_profile_id = uuid4()
    with clean_database.begin() as connection:
        connection.execute(
            text("INSERT INTO profiles (id, name) VALUES (:id, 'Second person')"),
            {"id": second_profile_id},
        )

    connection = clean_database.connect()
    transaction = connection.begin()
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    try:
        with pytest.raises(
            DBAPIError, match="non-default profiles would lose ownership"
        ):
            command.downgrade(config, "0003_review_corrections")
    finally:
        transaction.rollback()
        connection.close()

    with clean_database.begin() as cleanup_connection:
        revision = cleanup_connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )
        cleanup_connection.execute(
            text("DELETE FROM profiles WHERE id = :id"), {"id": second_profile_id}
        )

    assert revision == "0013_pdf_table_evidence"


def test_downgrade_refuses_to_collapse_multiple_source_occurrences(
    session: Session, clean_database: Engine
) -> None:
    document = make_document(session)
    second_source = SourceRecord(
        profile_id=DEFAULT_PROFILE_ID,
        provider="google_drive",
        external_id="drive-file-id",
        revision="sha256:1",
    )
    session.add(second_source)
    session.flush()
    session.add(
        DocumentSourceRecord(
            document_id=document.id,
            source_record_id=second_source.id,
            profile_id=DEFAULT_PROFILE_ID,
        )
    )
    session.commit()

    connection = clean_database.connect()
    transaction = connection.begin()
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    try:
        with pytest.raises(DBAPIError, match="multiple source occurrences"):
            command.downgrade(config, "0003_review_corrections")
    finally:
        transaction.rollback()
        connection.close()

    with clean_database.connect() as check_connection:
        revision = check_connection.scalar(
            text("SELECT version_num FROM alembic_version")
        )

    assert revision == "0013_pdf_table_evidence"


def test_fresh_migrations_can_downgrade_to_base_and_upgrade_again(
    clean_database: Engine,
) -> None:
    config = Config("alembic.ini")
    try:
        with clean_database.begin() as connection:
            config.attributes["connection"] = connection
            command.downgrade(config, "base")
            command.upgrade(config, "head")
            revision = connection.scalar(
                text("SELECT version_num FROM alembic_version")
            )
    finally:
        with clean_database.begin() as connection:
            config.attributes["connection"] = connection
            command.upgrade(config, "head")

    assert revision == "0013_pdf_table_evidence"


def test_observation_page_must_exist_in_its_document(session: Session) -> None:
    page_document = make_document(session)
    make_page(session, page_document)
    observation_document = make_document(session, identity="2")
    observation = LabObservation(
        document=observation_document,
        page_number=1,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        source_unit="ng/mL",
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=0.7,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    session.add(observation)

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


@pytest.mark.parametrize(
    ("page_number", "confidence", "reference_low", "reference_high"),
    [
        (0, 0.7, None, None),
        (1, 1.1, None, None),
        (1, 0.7, 40, 30),
    ],
)
def test_observation_values_must_satisfy_database_checks(
    session: Session,
    page_number: int,
    confidence: float,
    reference_low: int | None,
    reference_high: int | None,
) -> None:
    document = make_document(session)
    make_page(session, document)
    observation = LabObservation(
        document=document,
        page_number=page_number,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        source_unit="ng/mL",
        reference_low=reference_low,
        reference_high=reference_high,
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=confidence,
        status=ReviewStatus.NEEDS_REVIEW,
    )
    session.add(observation)

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_document_page_number_must_be_positive(session: Session) -> None:
    document = make_document(session)
    session.add(
        DocumentPage(
            document=document,
            page_number=0,
            extracted_text="text",
            extraction_method="digital_text",
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()
