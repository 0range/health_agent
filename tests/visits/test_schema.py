from datetime import UTC, datetime, timedelta

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import inspect
from sqlalchemy.exc import DBAPIError

from alembic import command
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Base
from health_agent.visits.repository import VisitRepository


def test_metadata_and_empty_migration_roundtrip(clean_database):
    config = Config("alembic.ini")
    with clean_database.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0008_lab_extraction")
        assert "health_visits" not in inspect(connection).get_table_names()
        command.upgrade(config, "0010_doctor_visits")
        assert (
            compare_metadata(MigrationContext.configure(connection), Base.metadata)
            == []
        )


def test_populated_downgrade_refuses_to_erase_visits(clean_database):
    start = datetime(2026, 10, 5, tzinfo=UTC)
    with session_scope(clean_database) as session:
        visit = VisitRepository(session).create(
            DEFAULT_PROFILE_ID,
            title="Fixture",
            starts_at=start,
            ends_at=start + timedelta(hours=1),
            timezone_name="UTC",
            creation_key="migration-test",
        )
    config = Config("alembic.ini")
    with (
        pytest.raises(DBAPIError, match="Refusing to downgrade visits"),
        clean_database.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "0008_lab_extraction")
    with session_scope(clean_database) as session:
        assert (
            VisitRepository(session).get(DEFAULT_PROFILE_ID, visit.public_code) == visit
        )
