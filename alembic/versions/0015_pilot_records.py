"""Durable user goals, diaries, meals and textual training plans."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_pilot_records"
down_revision = "0014_v01_workflow_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pilot_records",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id", sa.Uuid(), sa.ForeignKey("profiles.id"), nullable=False
        ),
        sa.Column("domain", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(500), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.UniqueConstraint("profile_id", "domain", "kind", "source_key"),
    )
    for column in ("profile_id", "domain", "at"):
        op.create_index(f"ix_pilot_records_{column}", "pilot_records", [column])


def downgrade() -> None:
    op.drop_table("pilot_records")
