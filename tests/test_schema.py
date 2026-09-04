from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import insert

from alembic import command
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.models import (
    Document,
    DocumentPage,
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


@pytest.fixture(scope="session")
def engine():
    settings = Settings()
    assert settings.database_url is not None
    alembic_config = Config("alembic.ini")
    alembic_config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(alembic_config, "head")
    return build_engine(settings)


@pytest.fixture
def session(engine) -> Iterator[Session]:
    with engine.begin() as connection:
        for table in (
            "review_items",
            "lab_observations",
            "document_pages",
            "documents",
            "source_records",
        ):
            connection.execute(text(f"DELETE FROM {table}"))
    with session_scope(engine) as database_session:
        yield database_session


def make_document(session: Session, identity: str = "1") -> Document:
    source = SourceRecord(
        provider="local_file",
        external_id=f"abc-{identity}",
        revision=f"sha256:{identity}",
    )
    document = Document(
        source_record=source,
        sha256=identity * 64,
        vault_path=f"data/vault/{identity}{identity}/" + identity * 64,
        media_type="application/pdf",
        document_type="laboratory_report",
        processing_status="pending",
    )
    session.add(document)
    session.flush()
    return document


def make_page(session: Session, document: Document, page_number: int = 1) -> DocumentPage:
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
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))
    session.flush()
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))

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
        source_unit="ng/mL",
        evidence_excerpt="Ferritin 42 ng/mL",
        confidence=0.7,
        status=ReviewStatus.NEEDS_REVIEW,
    )

    assert observation.is_publishable is False
    session.add(observation)


def test_verified_history_view_excludes_non_verified_observations(session: Session) -> None:
    document = make_document(session)
    make_page(session, document)
    verified = LabObservation(
        document=document,
        page_number=1,
        canonical_name="ferritin",
        source_name="Ferritin",
        source_value="42",
        source_unit="ng/mL",
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

    names = session.execute(
        text("SELECT canonical_name FROM verified_lab_history ORDER BY canonical_name")
    ).scalars().all()
    status = session.execute(
        text("SELECT status::text FROM lab_observations WHERE id = :observation_id"),
        {"observation_id": verified.id},
    ).scalar_one()

    assert names == ["ferritin"]
    assert status == "verified"


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
