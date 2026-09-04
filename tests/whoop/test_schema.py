from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.whoop.models import WhoopCycle, WhoopRawRecord
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    WhoopRepositoryError,
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
            resource_kind="cycle",
            external_id="crossed",
            start_at=datetime(2026, 9, 4, tzinfo=UTC),
            raw_record_id=first_raw.id,
            source_values={},
        )
    )

    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_raw_provenance_must_match_resource_kind_and_external_id(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    payload = cycle_payload()
    store_normalized_record(
        session,
        connection,
        normalize_whoop("cycle", payload),
        payload,
        datetime(2026, 9, 4, tzinfo=UTC),
    )
    raw = session.scalar(select(WhoopRawRecord))
    assert raw is not None
    session.add(
        WhoopCycle(
            profile_id=DEFAULT_PROFILE_ID,
            connection_id=connection.id,
            resource_kind="cycle",
            external_id="not-the-raw-id",
            start_at=datetime(2026, 9, 4, tzinfo=UTC),
            raw_record_id=raw.id,
            source_values={},
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
            "'whoop_workout_history', 'whoop_body_snapshot', "
            "'whoop_source_status') "
            "AND column_name = 'profile_id'"
        )
    ).all()

    assert {row.table_name for row in columns} == {
        "whoop_daily_health",
        "whoop_sleep_history",
        "whoop_workout_history",
        "whoop_body_snapshot",
        "whoop_source_status",
    }
    daily_columns = {
        row.column_name
        for row in session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'whoop_daily_health'"
            )
        )
    }
    source_columns = {
        row.column_name
        for row in session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'whoop_source_status'"
            )
        )
    }
    assert {
        "cycle_score_state",
        "recovery_score_state",
        "sleep_score_state",
        "user_calibrating",
    }.issubset(daily_columns)
    assert "recovery_count" in source_columns


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


def test_older_revision_is_archived_without_overwriting_current(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    newer = cycle_payload(9.5)
    older = {**cycle_payload(1.2), "updated_at": "2026-09-03T08:00:00Z"}

    first = store_normalized_record(
        session,
        connection,
        normalize_whoop("cycle", newer),
        newer,
        datetime(2026, 9, 4, tzinfo=UTC),
    )
    second = store_normalized_record(
        session,
        connection,
        normalize_whoop("cycle", older),
        older,
        datetime(2026, 9, 5, tzinfo=UTC),
    )

    current = session.scalar(select(WhoopCycle))
    assert first.normalized_created == 1
    assert second.raw_created == 1
    assert second.unchanged == 1
    assert current is not None
    assert current.strain == Decimal("9.5")
    assert session.scalar(select(func.count()).select_from(WhoopRawRecord)) == 2


@pytest.mark.parametrize("updated_at", ("2026-09-04T08:00:00Z", None))
def test_equal_or_missing_revision_times_use_deterministic_payload_rank(
    session: Session, updated_at: str | None
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    payloads = []
    for strain in (1.1, 8.8):
        payload = cycle_payload(strain)
        if updated_at is None:
            payload.pop("updated_at")
        else:
            payload["updated_at"] = updated_at
        payloads.append(payload)
    normalized = [normalize_whoop("cycle", payload) for payload in payloads]
    winner = max(normalized, key=lambda record: record.payload_hash)
    loser = min(normalized, key=lambda record: record.payload_hash)

    store_normalized_record(
        session,
        connection,
        winner,
        winner.values["source_values"],
        datetime(2026, 9, 4, tzinfo=UTC),
    )
    result = store_normalized_record(
        session,
        connection,
        loser,
        loser.values["source_values"],
        datetime(2026, 9, 5, tzinfo=UTC),
    )

    current = session.scalar(select(WhoopCycle))
    assert current is not None
    assert current.raw_record_id == session.scalar(
        select(WhoopRawRecord.id).where(
            WhoopRawRecord.payload_sha256 == winner.payload_hash
        )
    )
    assert result.unchanged == 1


def test_payload_user_must_match_connection_before_raw_is_written(
    session: Session,
) -> None:
    connection = register_authorized_connection(
        session, DEFAULT_PROFILE_ID, "main", 10129, ("read:cycles",)
    )
    payload = {**cycle_payload(), "user_id": 99999}

    with pytest.raises(WhoopRepositoryError, match="different account"):
        store_normalized_record(
            session,
            connection,
            normalize_whoop("cycle", payload),
            payload,
            datetime(2026, 9, 4, tzinfo=UTC),
        )

    assert session.scalar(select(func.count()).select_from(WhoopRawRecord)) == 0


def test_whoop_migration_matches_sqlalchemy_metadata(session: Session) -> None:
    config = Config("alembic.ini")
    connection = session.connection()
    config.attributes["connection"] = connection

    command.check(config)


def test_whoop_schema_has_final_pre_release_fingerprint(session: Session) -> None:
    columns = {
        (row.table_name, row.column_name)
        for row in session.execute(
            text(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE (table_name = 'whoop_connections' "
                "AND column_name IN ('token_generation', 'retry_at')) "
                "OR (table_name = 'whoop_sync_runs' AND column_name = 'retry_at') "
                "OR (table_name = 'whoop_recoveries' "
                "AND column_name IN ('resource_kind', 'source_values'))"
            )
        )
    }

    assert columns == {
        ("whoop_connections", "token_generation"),
        ("whoop_connections", "retry_at"),
        ("whoop_sync_runs", "retry_at"),
        ("whoop_recoveries", "resource_kind"),
        ("whoop_recoveries", "source_values"),
    }


def test_whoop_migration_round_trip(clean_database: Engine) -> None:
    config = Config("alembic.ini")
    with clean_database.begin() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_chart_integrity")
        command.upgrade(config, "head")
        command.check(config)


def test_whoop_downgrade_refuses_to_destroy_existing_data(
    clean_database: Engine,
) -> None:
    connection_id = uuid4()
    with clean_database.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO whoop_connections "
                "(id, profile_id, account_name, external_user_id, auth_status, "
                "granted_scopes) VALUES "
                "(:id, :profile_id, 'main', 10129, 'connected', '[]'::jsonb)"
            ),
            {"id": connection_id, "profile_id": DEFAULT_PROFILE_ID},
        )

    config = Config("alembic.ini")
    with (
        pytest.raises(DBAPIError, match="Refusing to downgrade"),
        clean_database.begin() as connection,
    ):
        config.attributes["connection"] = connection
        command.downgrade(config, "0004_chart_integrity")

    with clean_database.begin() as connection:
        assert (
            connection.scalar(
                text("SELECT count(*) FROM whoop_connections WHERE id = :id"),
                {"id": connection_id},
            )
            == 1
        )
        connection.execute(
            text("DELETE FROM whoop_connections WHERE id = :id"),
            {"id": connection_id},
        )
