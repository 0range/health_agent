"""Add profile-isolated WHOOP storage and dashboard views.

Revision ID: 0005_whoop
Revises: 0004_chart_integrity
Create Date: 2026-09-04
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_whoop"
down_revision: str | Sequence[str] | None = "0004_chart_integrity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _connection_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["connection_id", "profile_id"],
        ["whoop_connections.id", "whoop_connections.profile_id"],
        name=f"fk_{table}_connection_profile",
    )


def _raw_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["raw_record_id", "profile_id", "connection_id"],
        [
            "whoop_raw_records.id",
            "whoop_raw_records.profile_id",
            "whoop_raw_records.connection_id",
        ],
        name=f"fk_{table}_raw_profile_connection",
    )


def _normalized_identity(table: str) -> tuple[sa.ForeignKeyConstraint, ...]:
    return (_connection_fk(table), _raw_fk(table))


def upgrade() -> None:
    op.create_table(
        "whoop_connections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("account_name", sa.String(length=64), nullable=False),
        sa.Column("external_user_id", sa.BigInteger(), nullable=True),
        sa.Column("auth_status", sa.String(length=32), nullable=False),
        sa.Column(
            "granted_scopes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["profiles.id"], name="fk_whoop_connections_profile"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "account_name", name="uq_whoop_connection_profile_account"
        ),
        sa.UniqueConstraint(
            "profile_id",
            "external_user_id",
            name="uq_whoop_connection_profile_user",
        ),
        sa.UniqueConstraint(
            "id", "profile_id", name="uq_whoop_connection_id_profile"
        ),
    )
    op.create_index(
        "ix_whoop_connections_profile_id", "whoop_connections", ["profile_id"]
    )
    op.create_table(
        "whoop_sync_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_created", sa.Integer(), nullable=False),
        sa.Column("normalized_created", sa.Integer(), nullable=False),
        sa.Column("normalized_updated", sa.Integer(), nullable=False),
        sa.Column("unchanged", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        _connection_fk("whoop_sync_runs"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_whoop_sync_runs_profile_id", "whoop_sync_runs", ["profile_id"]
    )
    op.create_index(
        "ix_whoop_sync_runs_connection_id", "whoop_sync_runs", ["connection_id"]
    )
    op.create_table(
        "whoop_raw_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("resource_kind", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        _connection_fk("whoop_raw_records"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            "resource_kind",
            "external_id",
            "payload_sha256",
            name="uq_whoop_raw_revision",
        ),
        sa.UniqueConstraint(
            "id",
            "profile_id",
            "connection_id",
            name="uq_whoop_raw_id_profile_connection",
        ),
    )
    op.create_index(
        "ix_whoop_raw_records_profile_id", "whoop_raw_records", ["profile_id"]
    )
    op.create_index(
        "ix_whoop_raw_records_connection_id", "whoop_raw_records", ["connection_id"]
    )
    op.create_index(
        "ix_whoop_raw_origin",
        "whoop_raw_records",
        ["profile_id", "connection_id", "resource_kind", "external_id"],
    )
    _create_profile_current()
    _create_body_current()
    _create_cycles()
    _create_recoveries()
    _create_sleeps()
    _create_workouts()
    _create_views()


def _create_profile_current() -> None:
    table = "whoop_profile_current"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_user_id", sa.BigInteger(), nullable=False),
        sa.Column("email", sa.String(length=500), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        *_normalized_identity(table),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "connection_id", name="uq_whoop_profile_current_connection"
        ),
    )
    _normalized_indexes(table)


def _create_body_current() -> None:
    table = "whoop_body_current"
    op.create_table(
        table,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("height_meter", sa.Numeric(), nullable=True),
        sa.Column("weight_kilogram", sa.Numeric(), nullable=True),
        sa.Column("max_heart_rate", sa.Integer(), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        *_normalized_identity(table),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "connection_id", name="uq_whoop_body_current_connection"
        ),
    )
    _normalized_indexes(table)


def _history_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("connection_id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
    ]


def _history_constraints(table: str) -> tuple[sa.SchemaItem, ...]:
    return (
        *_normalized_identity(table),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id",
            "connection_id",
            "external_id",
            name=f"uq_{table}_origin",
        ),
    )


def _create_cycles() -> None:
    table = "whoop_cycles"
    op.create_table(
        table,
        *_history_columns(),
        sa.Column("external_user_id", sa.BigInteger(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_day", sa.Date(), nullable=True),
        sa.Column("timezone_offset", sa.String(length=16), nullable=True),
        sa.Column("score_state", sa.String(length=32), nullable=True),
        sa.Column("strain", sa.Numeric(), nullable=True),
        sa.Column("kilojoule", sa.Numeric(), nullable=True),
        sa.Column("average_heart_rate", sa.Integer(), nullable=True),
        sa.Column("max_heart_rate", sa.Integer(), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        *_history_constraints(table),
    )
    _normalized_indexes(table)


def _create_recoveries() -> None:
    table = "whoop_recoveries"
    op.create_table(
        table,
        *_history_columns(),
        sa.Column("cycle_id", sa.BigInteger(), nullable=False),
        sa.Column("sleep_id", sa.String(length=255), nullable=True),
        sa.Column("external_user_id", sa.BigInteger(), nullable=True),
        sa.Column("score_state", sa.String(length=32), nullable=True),
        sa.Column("user_calibrating", sa.Boolean(), nullable=True),
        sa.Column("recovery_score", sa.Numeric(), nullable=True),
        sa.Column("resting_heart_rate", sa.Integer(), nullable=True),
        sa.Column("hrv_rmssd_milli", sa.Numeric(), nullable=True),
        sa.Column("spo2_percentage", sa.Numeric(), nullable=True),
        sa.Column("skin_temp_celsius", sa.Numeric(), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        *_history_constraints(table),
    )
    _normalized_indexes(table)


def _create_sleeps() -> None:
    table = "whoop_sleeps"
    op.create_table(
        table,
        *_history_columns(),
        sa.Column("cycle_id", sa.BigInteger(), nullable=True),
        sa.Column("external_user_id", sa.BigInteger(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_day", sa.Date(), nullable=True),
        sa.Column("timezone_offset", sa.String(length=16), nullable=True),
        sa.Column("is_nap", sa.Boolean(), nullable=True),
        sa.Column("score_state", sa.String(length=32), nullable=True),
        sa.Column("sleep_performance_percentage", sa.Numeric(), nullable=True),
        sa.Column("sleep_consistency_percentage", sa.Numeric(), nullable=True),
        sa.Column("sleep_efficiency_percentage", sa.Numeric(), nullable=True),
        sa.Column("respiratory_rate", sa.Numeric(), nullable=True),
        sa.Column("total_in_bed_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_awake_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_no_data_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_light_sleep_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_slow_wave_sleep_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_rem_sleep_milli", sa.BigInteger(), nullable=True),
        sa.Column("total_sleep_milli", sa.BigInteger(), nullable=True),
        sa.Column("sleep_cycle_count", sa.Integer(), nullable=True),
        sa.Column("disturbance_count", sa.Integer(), nullable=True),
        sa.Column("baseline_needed_milli", sa.BigInteger(), nullable=True),
        sa.Column("sleep_debt_needed_milli", sa.BigInteger(), nullable=True),
        sa.Column("strain_needed_milli", sa.BigInteger(), nullable=True),
        sa.Column("nap_credit_milli", sa.BigInteger(), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        *_history_constraints(table),
    )
    _normalized_indexes(table)


def _create_workouts() -> None:
    table = "whoop_workouts"
    op.create_table(
        table,
        *_history_columns(),
        sa.Column("external_user_id", sa.BigInteger(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("local_day", sa.Date(), nullable=True),
        sa.Column("timezone_offset", sa.String(length=16), nullable=True),
        sa.Column("sport_id", sa.Integer(), nullable=True),
        sa.Column("sport_name", sa.String(length=255), nullable=True),
        sa.Column("score_state", sa.String(length=32), nullable=True),
        sa.Column("strain", sa.Numeric(), nullable=True),
        sa.Column("average_heart_rate", sa.Integer(), nullable=True),
        sa.Column("max_heart_rate", sa.Integer(), nullable=True),
        sa.Column("kilojoule", sa.Numeric(), nullable=True),
        sa.Column("percent_recorded", sa.Numeric(), nullable=True),
        sa.Column("distance_meter", sa.Numeric(), nullable=True),
        sa.Column("altitude_gain_meter", sa.Numeric(), nullable=True),
        sa.Column("altitude_change_meter", sa.Numeric(), nullable=True),
        sa.Column("zone_zero_milli", sa.BigInteger(), nullable=True),
        sa.Column("zone_one_milli", sa.BigInteger(), nullable=True),
        sa.Column("zone_two_milli", sa.BigInteger(), nullable=True),
        sa.Column("zone_three_milli", sa.BigInteger(), nullable=True),
        sa.Column("zone_four_milli", sa.BigInteger(), nullable=True),
        sa.Column("zone_five_milli", sa.BigInteger(), nullable=True),
        sa.Column("raw_record_id", sa.Uuid(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        *_history_constraints(table),
    )
    _normalized_indexes(table)


def _normalized_indexes(table: str) -> None:
    op.create_index(f"ix_{table}_profile_id", table, ["profile_id"])
    op.create_index(f"ix_{table}_connection_id", table, ["connection_id"])


def _create_views() -> None:
    op.execute(
        "CREATE VIEW whoop_daily_health AS "
        "SELECT c.profile_id, c.connection_id, c.local_day AS day, "
        "c.external_id AS cycle_id, c.strain, c.average_heart_rate, "
        "r.recovery_score, r.resting_heart_rate, r.hrv_rmssd_milli, "
        "r.spo2_percentage, r.skin_temp_celsius, "
        "s.external_id AS sleep_id, s.total_sleep_milli, "
        "s.sleep_performance_percentage, s.sleep_efficiency_percentage, "
        "s.respiratory_rate "
        "FROM whoop_cycles c "
        "LEFT JOIN whoop_recoveries r ON r.profile_id = c.profile_id "
        "AND r.connection_id = c.connection_id AND r.external_id = c.external_id "
        "LEFT JOIN whoop_sleeps s ON s.profile_id = c.profile_id "
        "AND s.connection_id = c.connection_id AND s.external_id = r.sleep_id"
    )
    op.execute(
        "CREATE VIEW whoop_sleep_history AS "
        "SELECT profile_id, connection_id, external_id, local_day AS day, "
        "start_at, end_at, is_nap, score_state, total_sleep_milli, "
        "total_awake_milli, total_light_sleep_milli, total_slow_wave_sleep_milli, "
        "total_rem_sleep_milli, sleep_performance_percentage, "
        "sleep_consistency_percentage, sleep_efficiency_percentage, respiratory_rate "
        "FROM whoop_sleeps"
    )
    op.execute(
        "CREATE VIEW whoop_workout_history AS "
        "SELECT profile_id, connection_id, external_id, local_day AS day, "
        "start_at, end_at, sport_id, sport_name, score_state, strain, "
        "average_heart_rate, max_heart_rate, kilojoule, distance_meter, "
        "altitude_gain_meter FROM whoop_workouts"
    )
    op.execute(
        "CREATE VIEW whoop_source_status AS "
        "SELECT c.profile_id, c.id AS connection_id, c.account_name, c.auth_status, "
        "c.last_attempt_at, c.last_success_at, c.last_error_code, "
        "(SELECT count(*) FROM whoop_cycles x WHERE x.profile_id = c.profile_id "
        "AND x.connection_id = c.id) AS cycle_count, "
        "(SELECT count(*) FROM whoop_sleeps x WHERE x.profile_id = c.profile_id "
        "AND x.connection_id = c.id) AS sleep_count, "
        "(SELECT count(*) FROM whoop_workouts x WHERE x.profile_id = c.profile_id "
        "AND x.connection_id = c.id) AS workout_count "
        "FROM whoop_connections c"
    )


def downgrade() -> None:
    op.execute("DROP VIEW whoop_source_status")
    op.execute("DROP VIEW whoop_workout_history")
    op.execute("DROP VIEW whoop_sleep_history")
    op.execute("DROP VIEW whoop_daily_health")
    for table in (
        "whoop_workouts",
        "whoop_sleeps",
        "whoop_recoveries",
        "whoop_cycles",
        "whoop_body_current",
        "whoop_profile_current",
    ):
        op.drop_table(table)
    op.drop_index("ix_whoop_raw_origin", table_name="whoop_raw_records")
    op.drop_table("whoop_raw_records")
    op.drop_table("whoop_sync_runs")
    op.drop_table("whoop_connections")
