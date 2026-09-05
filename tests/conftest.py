from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from alembic import command
from health_agent.config import Settings
from health_agent.db import build_engine, session_scope

_DATABASE_PATTERN = re.compile(r"test_health_agent_[0-9a-f]{32}")
_CONTAINER_PATTERN = re.compile(r"health-agent-pytest-[0-9a-f]{32}")
_TABLES_IN_DELETE_ORDER = (
    "health_reminder_events",
    "health_reminders",
    "whoop_profile_current",
    "whoop_body_current",
    "whoop_cycles",
    "whoop_recoveries",
    "whoop_sleeps",
    "whoop_workouts",
    "whoop_raw_records",
    "whoop_sync_runs",
    "whoop_connections",
    "review_items",
    "lab_observations",
    "document_pages",
    "document_source_records",
    "source_records",
    "documents",
    "profiles",
)


@dataclass(frozen=True, slots=True)
class DisposablePostgres:
    database_name: str
    container_name: str
    settings: Settings
    engine: Engine


def require_disposable_database(database_name: str) -> None:
    if _DATABASE_PATTERN.fullmatch(database_name) is None:
        raise RuntimeError(f"Refusing destructive operation on {database_name!r}")


def _require_disposable_container(container_name: str) -> None:
    if _CONTAINER_PATTERN.fullmatch(container_name) is None:
        raise RuntimeError(f"Refusing to remove container {container_name!r}")


@pytest.fixture(scope="session")
def disposable_postgres() -> Iterator[DisposablePostgres]:
    suffix = uuid4().hex
    database_name = f"test_health_agent_{suffix}"
    container_name = f"health-agent-pytest-{suffix}"
    require_disposable_database(database_name)
    _require_disposable_container(container_name)
    started = False
    engine: Engine | None = None

    try:
        subprocess.run(
            (
                "docker",
                "run",
                "--detach",
                "--rm",
                "--name",
                container_name,
                "--publish",
                "127.0.0.1::5432",
                "--env",
                "POSTGRES_DB=" + database_name,
                "--env",
                "POSTGRES_USER=health_agent",
                "--env",
                "POSTGRES_PASSWORD=health_agent_test",
                "postgres:18.6-alpine",
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        started = True
        port_result = subprocess.run(
            (
                "docker",
                "inspect",
                "--format",
                '{{(index (index .NetworkSettings.Ports "5432/tcp") 0).HostPort}}',
                container_name,
            ),
            check=True,
            capture_output=True,
            text=True,
        )
        postgres_port = int(port_result.stdout.strip())
        settings = Settings(
            postgres_host="127.0.0.1",
            postgres_port=postgres_port,
            postgres_database=database_name,
            postgres_user="health_agent",
            postgres_password="health_agent_test",
            database_url=(
                "postgresql+psycopg://health_agent:health_agent_test@"
                f"127.0.0.1:{postgres_port}/{database_name}"
            ),
        )
        engine = build_engine(settings)
        _wait_for_postgres(engine)
        alembic_config = Config("alembic.ini")
        with engine.begin() as migration_connection:
            alembic_config.attributes["connection"] = migration_connection
            command.upgrade(alembic_config, "head")
        yield DisposablePostgres(
            database_name=database_name,
            container_name=container_name,
            settings=settings,
            engine=engine,
        )
    finally:
        if engine is not None:
            engine.dispose()
        if started:
            _require_disposable_container(container_name)
            subprocess.run(
                ("docker", "rm", "--force", container_name),
                check=True,
                capture_output=True,
                text=True,
            )


def _wait_for_postgres(engine: Engine) -> None:
    deadline = time.monotonic() + 30
    last_error: OperationalError | None = None
    while time.monotonic() < deadline:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            return
        except OperationalError as error:
            last_error = error
            time.sleep(0.1)
    raise RuntimeError("Disposable PostgreSQL did not become ready") from last_error


@pytest.fixture(scope="session")
def engine(disposable_postgres: DisposablePostgres) -> Engine:
    return disposable_postgres.engine


@pytest.fixture
def clean_database(disposable_postgres: DisposablePostgres) -> Engine:
    require_disposable_database(disposable_postgres.database_name)
    assert disposable_postgres.engine.url.database == disposable_postgres.database_name
    with disposable_postgres.engine.begin() as connection:
        for table in _TABLES_IN_DELETE_ORDER:
            connection.execute(text(f"DELETE FROM {table}"))
        connection.execute(
            text(
                "INSERT INTO profiles (id, name) VALUES "
                "('00000000-0000-0000-0000-000000000001', 'Default')"
            )
        )
    return disposable_postgres.engine


@pytest.fixture
def session(clean_database: Engine) -> Iterator[Session]:
    with session_scope(clean_database) as database_session:
        yield database_session
