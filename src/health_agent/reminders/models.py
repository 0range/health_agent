"""ORM and immutable public contracts for confirmed reminders."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from health_agent.models import Base


class ReminderStatus(StrEnum):
    PENDING_CONFIRMATION = "pending_confirmation"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class HealthReminder(Base):
    __tablename__ = "health_reminders"
    __table_args__ = (
        UniqueConstraint("id", "profile_id", name="uq_health_reminders_id_profile"),
        UniqueConstraint("public_code", name="uq_health_reminders_public_code"),
        UniqueConstraint(
            "recurrence_parent_id", name="uq_health_reminders_recurrence_parent"
        ),
        ForeignKeyConstraint(
            ["recurrence_parent_id", "profile_id"],
            ["health_reminders.id", "health_reminders.profile_id"],
            name="fk_health_reminders_recurrence_parent_profile",
        ),
        CheckConstraint(
            "status IN ('pending_confirmation', 'scheduled', 'completed', 'cancelled')",
            name="ck_health_reminders_status",
        ),
        CheckConstraint(
            "(status = 'pending_confirmation' AND confirmed_at IS NULL) OR "
            "(status IN ('scheduled', 'completed') AND confirmed_at IS NOT NULL) OR "
            "status = 'cancelled'",
            name="ck_health_reminders_confirmation",
        ),
        CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('pending_confirmation', 'scheduled') AND completed_at IS NULL "
            "AND cancelled_at IS NULL)",
            name="ck_health_reminders_terminal_timestamps",
        ),
        CheckConstraint(
            "delivery_revision >= 1", name="ck_health_reminders_delivery_revision"
        ),
        CheckConstraint(
            "(repeat_unit IS NULL AND repeat_every IS NULL) OR "
            "(repeat_unit IS NOT NULL AND repeat_every IS NOT NULL AND "
            "((repeat_unit = 'days' AND repeat_every BETWEEN 1 AND 3650) OR "
            "(repeat_unit = 'months' AND repeat_every BETWEEN 1 AND 120)))",
            name="ck_health_reminders_recurrence",
        ),
        CheckConstraint(
            "recurrence_parent_id IS NULL OR recurrence_parent_id <> id",
            name="ck_health_reminders_recurrence_not_self",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    public_code: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(500))
    reason: Mapped[str] = mapped_column(Text)
    source_type: Mapped[str] = mapped_column(String(100))
    source_reference: Mapped[str] = mapped_column(String(2000))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    timezone_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(
        String(32), default=ReminderStatus.PENDING_CONFIRMATION.value, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    proposal_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_revision: Mapped[int] = mapped_column(Integer, default=1)
    repeat_unit: Mapped[str | None] = mapped_column(String(10))
    repeat_every: Mapped[int | None] = mapped_column(Integer)
    recurrence_parent_id: Mapped[UUID | None] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    events: Mapped[list[HealthReminderEvent]] = relationship(
        back_populates="reminder", cascade="all, delete-orphan"
    )


class HealthReminderEvent(Base):
    __tablename__ = "health_reminder_events"
    __table_args__ = (
        UniqueConstraint("action_key", name="uq_health_reminder_events_action_key"),
        ForeignKeyConstraint(
            ["reminder_id", "profile_id"],
            ["health_reminders.id", "health_reminders.profile_id"],
            name="fk_health_reminder_events_reminder_profile",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    reminder_id: Mapped[UUID] = mapped_column(index=True)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    event_type: Mapped[str] = mapped_column(String(100))
    action_key: Mapped[str | None] = mapped_column(String(200))
    event_data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    reminder: Mapped[HealthReminder] = relationship(back_populates="events")


@dataclass(frozen=True, slots=True)
class Reminder:
    id: UUID
    profile_id: UUID
    public_code: str
    title: str
    reason: str
    source_type: str
    source_reference: str
    due_at: datetime
    timezone_name: str
    status: ReminderStatus
    confirmed_at: datetime | None
    proposal_notified_at: datetime | None
    delivered_at: datetime | None
    completed_at: datetime | None
    cancelled_at: datetime | None
    delivery_revision: int
    repeat_unit: str | None = None
    repeat_every: int | None = None
    recurrence_parent_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class ReminderStatusSummary:
    total: int
    pending_confirmation: int
    scheduled: int
    due: int
    delivered: int
    completed: int
    cancelled: int
