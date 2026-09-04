"""Enforce page evidence and numeric data invariants.

Revision ID: 0002_integrity_constraints
Revises: 0001_medical_core
Create Date: 2026-09-04
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002_integrity_constraints"
down_revision: str | Sequence[str] | None = "0001_medical_core"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_document_pages_page_number_positive", "document_pages", "page_number >= 1"
    )
    op.create_foreign_key(
        "fk_lab_observations_document_page",
        "lab_observations",
        "document_pages",
        ["document_id", "page_number"],
        ["document_id", "page_number"],
    )
    op.create_check_constraint(
        "ck_lab_observations_page_number_positive", "lab_observations", "page_number >= 1"
    )
    op.create_check_constraint(
        "ck_lab_observations_confidence_range",
        "lab_observations",
        "confidence >= 0 AND confidence <= 1",
    )
    op.create_check_constraint(
        "ck_lab_observations_reference_range",
        "lab_observations",
        "reference_low IS NULL OR reference_high IS NULL OR reference_low <= reference_high",
    )


def downgrade() -> None:
    op.drop_constraint("ck_lab_observations_reference_range", "lab_observations", type_="check")
    op.drop_constraint("ck_lab_observations_confidence_range", "lab_observations", type_="check")
    op.drop_constraint("ck_lab_observations_page_number_positive", "lab_observations", type_="check")
    op.drop_constraint("fk_lab_observations_document_page", "lab_observations", type_="foreignkey")
    op.drop_constraint("ck_document_pages_page_number_positive", "document_pages", type_="check")
