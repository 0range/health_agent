"""Durable explicit visit Calendar publication."""

import sqlalchemy as sa

from alembic import op

revision = "0012_visit_calendar"
down_revision = "0011_medical_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "visit_calendar_publications",
        sa.Column("visit_id", sa.Uuid(), primary_key=True),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("target_subject", sa.String(500)),
        sa.Column("target_calendar", sa.String(1024)),
        sa.Column("successful_fingerprint", sa.String(64)),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("safe_error", sa.String(64)),
        sa.Column("html_link", sa.String(2048)),
        sa.ForeignKeyConstraint(
            ["visit_id", "profile_id"],
            ["health_visits.id", "health_visits.profile_id"],
            name="fk_visit_calendar_owner",
        ),
        sa.CheckConstraint(
            "status IN ('queued','published')", name="ck_visit_calendar_status"
        ),
    )
    op.create_index(
        "ix_visit_calendar_publications_profile_id",
        "visit_calendar_publications",
        ["profile_id"],
    )


def downgrade() -> None:
    op.drop_table("visit_calendar_publications")
