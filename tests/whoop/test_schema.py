from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.whoop.models import WhoopCycle, WhoopRawRecord
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)


def cycle_payload(strain: float = 5.2) -> dict[str, object]:
    return {
        "id": 93845,
        "user_id": 10129,
        "updated_at": "2026-09-04T08:00:00Z",
        "start": "2026-09-03T22:00:00Z",
        "end": "2026-09-04T08:00:00Z",
        "timezone_offset": "+03:00",
        "score_state": "SCORED",
        "score": {"strain": strain, "average_heart_rate": 68},
    }


def test_same_whoop_ids_are_isolated_between_profiles(session: Session) -> None:
    second_profile = Profile(id=uuid4(), name="Second person")
    session.add(second_profile)
    session.flush()
    first = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    second = register_authorized_connection(
        session, second_profile.id, "main", 10129, ("read:cycles",)
    )
    payload = cycle_payload()
    normalized = normalize_whoop("cycle", payload)
    fetched_at = datetime(2026, 9, 4, tzinfo=UTC)

    store_normalized_record(session, first, normalized, payload, fetched_at)
    store_normalized_record(session, second, normalized, payload, fetched_at)

    rows = session.execute(
        select(WhoopCycle.profile_id, WhoopCycle.external_id).order_by(
            WhoopCycle.profile_id
        )
    ).all()
    assert len(rows) == 2
    assert {row.profile_id for row in rows} == {DEFAULT_PROFILE_ID, second_profile.id}
    assert {row.external_id for row in rows} == {"93845"}


def test_cross_profile_raw_provenance_is_rejected(session: Session) -> None:
    second_profile = Profile(id=uuid4(), name="Second person")
    session.add(second_profile)
    session.flush()
    first = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    second = register_authorized_connection(
        session, second_profile.id, "main", 20258, ("read:cycles",)
    )
    payload = cycle_payload()
    normalized = normalize_whoop("cycle", payload)
    store_normalized_record(
        session, first, normalized, payload, datetime(2026, 9, 4, tzinfo=UTC)
    )
    first_raw = session.scalar(select(WhoopRawRecord))
    assert first_raw is not None
    session.add(
        WhoopCycle(
            profile_id=second_profile.id,
            connection_id=second.id,
            external_id="crossed",
            start_at=datetime(2026, 9, 4, tzinfo=UTC),
            raw_record_id=first_raw.id,
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_dashboard_views_keep_profile_dimension(session: Session) -> None:
    columns = session.execute(
        text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_name LIKE 'whoop_%' AND table_name IN "
            "('whoop_daily_health', 'whoop_sleep_history', "
            "'whoop_workout_history', 'whoop_source_status') "
            "AND column_name = 'profile_id'"
        )
    ).all()

    assert {row.table_name for row in columns} == {
        "whoop_daily_health",
        "whoop_sleep_history",
        "whoop_workout_history",
        "whoop_source_status",
    }


def test_raw_revision_is_idempotent(session: Session) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    payload = cycle_payload()
    normalized = normalize_whoop("cycle", payload)
    fetched_at = datetime(2026, 9, 4, tzinfo=UTC)

    first = store_normalized_record(
        session, connection, normalized, payload, fetched_at
    )
    second = store_normalized_record(
        session, connection, normalized, payload, fetched_at
    )

    assert first.raw_created == 1
    assert second.raw_created == 0
    assert session.scalar(select(func.count()).select_from(WhoopRawRecord)) == 1


def test_whoop_migration_matches_sqlalchemy_metadata(session: Session) -> None:
    config = Config("alembic.ini")
    connection = session.connection()
    config.attributes["connection"] = connection

    command.check(config)


def test_whoop_migration_round_trip(engine: Engine) -> None:
    config = Config("alembic.ini")
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_chart_integrity")
        command.upgrade(config, "head")
        command.check(config)
