"""Add immutable correction lineage for reviewed observations.

Revision ID: 0003_review_corrections
Revises: 0002_integrity_constraints
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_review_corrections"
down_revision: str | Sequence[str] | None = "0002_integrity_constraints"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lab_observations",
        sa.Column("supersedes_observation_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_lab_observations_supersedes_observation",
        "lab_observations",
        "lab_observations",
        ["supersedes_observation_id"],
        ["id"],
    )
    op.create_index(
        "ix_lab_observations_supersedes_observation_id",
        "lab_observations",
        ["supersedes_observation_id"],
    )


def downgrade() -> None:
    # 0004 recreates this legacy SELECT * view before returning to 0003. Drop it
    # while removing the referenced column, then restore the exact 0002 shape.
    op.execute("DROP VIEW IF EXISTS verified_lab_history")
    op.drop_index(
        "ix_lab_observations_supersedes_observation_id", table_name="lab_observations"
    )
    op.drop_constraint(
        "fk_lab_observations_supersedes_observation",
        "lab_observations",
        type_="foreignkey",
    )
    op.drop_column("lab_observations", "supersedes_observation_id")
    op.execute(
        "CREATE VIEW verified_lab_history AS "
        "SELECT * FROM lab_observations WHERE status = 'verified'"
    )
