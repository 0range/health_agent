from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from health_agent.db import session_scope
from health_agent.lab_extraction.models import LabExtractionJob, LabExtractionProfile
from health_agent.models import DEFAULT_PROFILE_ID, Base, Profile
from lab_extraction.test_service import add_page


def test_queue_foreign_profile_and_missing_page_are_rejected(clean_database):
    other = uuid4()
    document_id = add_page(clean_database)
    with session_scope(clean_database) as session:
        session.add(Profile(id=other, name="Synthetic other"))
        session.flush()
        session.add_all(
            [
                LabExtractionProfile(profile_id=DEFAULT_PROFILE_ID),
                LabExtractionProfile(profile_id=other),
            ]
        )
    for profile_id, page in ((other, 1), (DEFAULT_PROFILE_ID, 2)):
        with pytest.raises(IntegrityError), session_scope(clean_database) as session:
            session.add(
                LabExtractionJob(
                    profile_id=profile_id, document_id=document_id, page_number=page
                )
            )


def test_migrated_metadata_matches_models(clean_database):
    with clean_database.connect() as connection:
        differences = compare_metadata(
            MigrationContext.configure(connection), Base.metadata
        )
    assert differences == []


def test_downgrade_cannot_erase_budget_and_unknown_outcome_fences(clean_database):
    with session_scope(clean_database) as session:
        session.add(LabExtractionProfile(profile_id=DEFAULT_PROFILE_ID))
    config = Config("alembic.ini")
    with (
        pytest.raises(DBAPIError, match="Refusing to downgrade lab extraction"),
        clean_database.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "0007_google_sheets")
