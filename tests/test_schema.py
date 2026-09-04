from __future__ import annotations

from collections.abc import Iterator

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope
from health_agent.models import Document, LabObservation, ReviewStatus, SourceRecord


def test_settings_builds_a_local_database_url() -> None:
    settings = Settings(postgres_password="space password")

    assert settings.database_url == (
        "postgresql+psycopg://health_agent:space%20password@127.0.0.1:55432/health_agent"
    )


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


def make_document(session: Session) -> Document:
    source = SourceRecord(
        provider="local_file",
        external_id="abc",
        revision="sha256:1",
    )
    document = Document(
        source_record=source,
        sha256="1" * 64,
        vault_path="data/vault/11/" + "1" * 64,
        media_type="application/pdf",
        document_type="laboratory_report",
        processing_status="pending",
    )
    session.add(document)
    session.flush()
    return document


def test_same_source_revision_is_unique(session: Session) -> None:
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))
    session.flush()
    session.add(SourceRecord(provider="local_file", external_id="abc", revision="sha256:1"))

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_review_required_observation_is_not_publishable(session: Session) -> None:
    document = make_document(session)
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


def test_verified_history_view_excludes_non_verified_observations(session: Session) -> None:
    document = make_document(session)
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

    assert names == ["ferritin"]
