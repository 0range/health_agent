from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from health_agent.models import Base


def _current_constraints(resource_kind: str) -> tuple[Any, ...]:
    return (
        ForeignKeyConstraint(
            ["connection_id", "profile_id"],
            ["whoop_connections.id", "whoop_connections.profile_id"],
        ),
        ForeignKeyConstraint(
            [
                "raw_record_id",
                "profile_id",
                "connection_id",
                "resource_kind",
                "external_id",
            ],
            [
                "whoop_raw_records.id",
                "whoop_raw_records.profile_id",
                "whoop_raw_records.connection_id",
                "whoop_raw_records.resource_kind",
                "whoop_raw_records.external_id",
            ],
        ),
        CheckConstraint(
            f"resource_kind = '{resource_kind}'",
            name=f"ck_whoop_{resource_kind}_current_kind",
        ),
        UniqueConstraint("profile_id", "connection_id"),
    )


def _history_constraints(resource_kind: str) -> tuple[Any, ...]:
    return (
        ForeignKeyConstraint(
            ["connection_id", "profile_id"],
            ["whoop_connections.id", "whoop_connections.profile_id"],
        ),
        ForeignKeyConstraint(
            [
                "raw_record_id",
                "profile_id",
                "connection_id",
                "resource_kind",
                "external_id",
            ],
            [
                "whoop_raw_records.id",
                "whoop_raw_records.profile_id",
                "whoop_raw_records.connection_id",
                "whoop_raw_records.resource_kind",
                "whoop_raw_records.external_id",
            ],
        ),
        CheckConstraint(
            f"resource_kind = '{resource_kind}'",
            name=f"ck_whoop_{resource_kind}_kind",
        ),
        UniqueConstraint("profile_id", "connection_id", "external_id"),
    )


class WhoopConnection(Base):
    __tablename__ = "whoop_connections"
    __table_args__ = (
        UniqueConstraint("profile_id", "account_name"),
        UniqueConstraint("profile_id", "external_user_id"),
        UniqueConstraint("id", "profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    account_name: Mapped[str] = mapped_column(String(64))
    external_user_id: Mapped[int | None] = mapped_column(BigInteger)
    auth_status: Mapped[str] = mapped_column(String(32), default="connected")
    granted_scopes: Mapped[list[str]] = mapped_column(JSONB, default=list)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class WhoopSyncRun(Base):
    __tablename__ = "whoop_sync_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "profile_id"],
            ["whoop_connections.id", "whoop_connections.profile_id"],
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    mode: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="running")
    requested_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_created: Mapped[int] = mapped_column(default=0)
    normalized_created: Mapped[int] = mapped_column(default=0)
    normalized_updated: Mapped[int] = mapped_column(default=0)
    unchanged: Mapped[int] = mapped_column(default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(100))


class WhoopRawRecord(Base):
    __tablename__ = "whoop_raw_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["connection_id", "profile_id"],
            ["whoop_connections.id", "whoop_connections.profile_id"],
        ),
        UniqueConstraint(
            "profile_id",
            "connection_id",
            "resource_kind",
            "external_id",
            "payload_sha256",
        ),
        UniqueConstraint(
            "id",
            "profile_id",
            "connection_id",
            "resource_kind",
            "external_id",
        ),
        Index(
            "ix_whoop_raw_origin",
            "profile_id",
            "connection_id",
            "resource_kind",
            "external_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(255))
    payload_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WhoopProfileCurrent(Base):
    __tablename__ = "whoop_profile_current"
    __table_args__ = _current_constraints("profile")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="profile")
    external_id: Mapped[str] = mapped_column(String(255))
    external_user_id: Mapped[int] = mapped_column(BigInteger)
    email: Mapped[str | None] = mapped_column(String(500))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    raw_record_id: Mapped[UUID]
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WhoopBodyCurrent(Base):
    __tablename__ = "whoop_body_current"
    __table_args__ = _current_constraints("body")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="body")
    external_id: Mapped[str] = mapped_column(String(255), default="current")
    height_meter: Mapped[Decimal | None] = mapped_column(Numeric)
    weight_kilogram: Mapped[Decimal | None] = mapped_column(Numeric)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    raw_record_id: Mapped[UUID]
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WhoopCycle(Base):
    __tablename__ = "whoop_cycles"
    __table_args__ = _history_constraints("cycle")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="cycle")
    external_id: Mapped[str] = mapped_column(String(255))
    external_user_id: Mapped[int | None] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_day: Mapped[date | None]
    timezone_offset: Mapped[str | None] = mapped_column(String(16))
    score_state: Mapped[str | None] = mapped_column(String(32))
    strain: Mapped[Decimal | None] = mapped_column(Numeric)
    kilojoule: Mapped[Decimal | None] = mapped_column(Numeric)
    average_heart_rate: Mapped[int | None] = mapped_column(Integer)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    raw_record_id: Mapped[UUID]
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WhoopRecovery(Base):
    __tablename__ = "whoop_recoveries"
    __table_args__ = _history_constraints("recovery")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="recovery")
    external_id: Mapped[str] = mapped_column(String(255))
    cycle_id: Mapped[int] = mapped_column(BigInteger)
    sleep_id: Mapped[str | None] = mapped_column(String(255))
    external_user_id: Mapped[int | None] = mapped_column(BigInteger)
    score_state: Mapped[str | None] = mapped_column(String(32))
    user_calibrating: Mapped[bool | None] = mapped_column(Boolean)
    recovery_score: Mapped[Decimal | None] = mapped_column(Numeric)
    resting_heart_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    hrv_rmssd_milli: Mapped[Decimal | None] = mapped_column(Numeric)
    spo2_percentage: Mapped[Decimal | None] = mapped_column(Numeric)
    skin_temp_celsius: Mapped[Decimal | None] = mapped_column(Numeric)
    raw_record_id: Mapped[UUID]
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WhoopSleep(Base):
    __tablename__ = "whoop_sleeps"
    __table_args__ = _history_constraints("sleep")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="sleep")
    external_id: Mapped[str] = mapped_column(String(255))
    cycle_id: Mapped[int | None] = mapped_column(BigInteger)
    external_user_id: Mapped[int | None] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_day: Mapped[date | None]
    timezone_offset: Mapped[str | None] = mapped_column(String(16))
    is_nap: Mapped[bool | None] = mapped_column(Boolean)
    score_state: Mapped[str | None] = mapped_column(String(32))
    sleep_performance_percentage: Mapped[Decimal | None] = mapped_column(Numeric)
    sleep_consistency_percentage: Mapped[Decimal | None] = mapped_column(Numeric)
    sleep_efficiency_percentage: Mapped[Decimal | None] = mapped_column(Numeric)
    respiratory_rate: Mapped[Decimal | None] = mapped_column(Numeric)
    total_in_bed_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_awake_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_no_data_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_light_sleep_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_slow_wave_sleep_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_rem_sleep_milli: Mapped[int | None] = mapped_column(BigInteger)
    total_sleep_milli: Mapped[int | None] = mapped_column(BigInteger)
    sleep_cycle_count: Mapped[int | None] = mapped_column(Integer)
    disturbance_count: Mapped[int | None] = mapped_column(Integer)
    baseline_needed_milli: Mapped[int | None] = mapped_column(BigInteger)
    sleep_debt_needed_milli: Mapped[int | None] = mapped_column(BigInteger)
    strain_needed_milli: Mapped[int | None] = mapped_column(BigInteger)
    nap_credit_milli: Mapped[int | None] = mapped_column(BigInteger)
    raw_record_id: Mapped[UUID]
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)


class WhoopWorkout(Base):
    __tablename__ = "whoop_workouts"
    __table_args__ = _history_constraints("workout")

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    connection_id: Mapped[UUID] = mapped_column(index=True)
    resource_kind: Mapped[str] = mapped_column(String(32), default="workout")
    external_id: Mapped[str] = mapped_column(String(255))
    external_user_id: Mapped[int | None] = mapped_column(BigInteger)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    local_day: Mapped[date | None]
    timezone_offset: Mapped[str | None] = mapped_column(String(16))
    sport_id: Mapped[int | None] = mapped_column(Integer)
    sport_name: Mapped[str | None] = mapped_column(String(255))
    score_state: Mapped[str | None] = mapped_column(String(32))
    strain: Mapped[Decimal | None] = mapped_column(Numeric)
    average_heart_rate: Mapped[int | None] = mapped_column(Integer)
    max_heart_rate: Mapped[int | None] = mapped_column(Integer)
    kilojoule: Mapped[Decimal | None] = mapped_column(Numeric)
    percent_recorded: Mapped[Decimal | None] = mapped_column(Numeric)
    distance_meter: Mapped[Decimal | None] = mapped_column(Numeric)
    altitude_gain_meter: Mapped[Decimal | None] = mapped_column(Numeric)
    altitude_change_meter: Mapped[Decimal | None] = mapped_column(Numeric)
    zone_zero_milli: Mapped[int | None] = mapped_column(BigInteger)
    zone_one_milli: Mapped[int | None] = mapped_column(BigInteger)
    zone_two_milli: Mapped[int | None] = mapped_column(BigInteger)
    zone_three_milli: Mapped[int | None] = mapped_column(BigInteger)
    zone_four_milli: Mapped[int | None] = mapped_column(BigInteger)
    zone_five_milli: Mapped[int | None] = mapped_column(BigInteger)
    raw_record_id: Mapped[UUID]
    source_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_values: Mapped[dict[str, Any]] = mapped_column(JSONB)
