"""Add profile-scoped confirmed health reminders.

Revision ID: 0006_health_reminders
Revises: 0005_whoop
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_health_reminders"
down_revision: str | Sequence[str] | None = "0005_whoop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "health_reminders",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("public_code", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("source_type", sa.String(length=100), nullable=False),
        sa.Column("source_reference", sa.String(length=2000), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_name", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("proposal_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivery_revision", sa.Integer(), nullable=False),
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
            ["profile_id"], ["profiles.id"], name="fk_health_reminders_profile"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "profile_id", name="uq_health_reminders_id_profile"),
        sa.UniqueConstraint("public_code", name="uq_health_reminders_public_code"),
        sa.CheckConstraint(
            "status IN ('pending_confirmation', 'scheduled', 'completed', 'cancelled')",
            name="ck_health_reminders_status",
        ),
        sa.CheckConstraint(
            "(status = 'pending_confirmation' AND confirmed_at IS NULL) OR "
            "(status IN ('scheduled', 'completed') AND confirmed_at IS NOT NULL) OR "
            "status = 'cancelled'",
            name="ck_health_reminders_confirmation",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND completed_at IS NOT NULL AND cancelled_at IS NULL) OR "
            "(status = 'cancelled' AND cancelled_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status IN ('pending_confirmation', 'scheduled') AND completed_at IS NULL "
            "AND cancelled_at IS NULL)",
            name="ck_health_reminders_terminal_timestamps",
        ),
        sa.CheckConstraint(
            "delivery_revision >= 1", name="ck_health_reminders_delivery_revision"
        ),
    )
    op.create_index(
        "ix_health_reminders_profile_id", "health_reminders", ["profile_id"]
    )
    op.create_index("ix_health_reminders_due_at", "health_reminders", ["due_at"])
    op.create_index("ix_health_reminders_status", "health_reminders", ["status"])
    op.create_table(
        "health_reminder_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reminder_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("action_key", sa.String(length=200), nullable=True),
        sa.Column(
            "event_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["reminder_id", "profile_id"],
            ["health_reminders.id", "health_reminders.profile_id"],
            name="fk_health_reminder_events_reminder_profile",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "action_key", name="uq_health_reminder_events_action_key"
        ),
    )
    op.create_index(
        "ix_health_reminder_events_reminder_id",
        "health_reminder_events",
        ["reminder_id"],
    )
    op.create_index(
        "ix_health_reminder_events_profile_id", "health_reminder_events", ["profile_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_health_reminder_events_profile_id", table_name="health_reminder_events"
    )
    op.drop_index(
        "ix_health_reminder_events_reminder_id", table_name="health_reminder_events"
    )
    op.drop_table("health_reminder_events")
    op.drop_index("ix_health_reminders_status", table_name="health_reminders")
    op.drop_index("ix_health_reminders_due_at", table_name="health_reminders")
    op.drop_index("ix_health_reminders_profile_id", table_name="health_reminders")
    op.drop_table("health_reminders")
