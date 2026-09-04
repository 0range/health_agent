"""Create provenance-first medical records and verified dashboard history.

Revision ID: 0001_medical_core
Revises:
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_medical_core"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


review_status = sa.Enum("verified", "needs_review", "rejected", name="review_status")


def upgrade() -> None:
    op.create_table(
        "source_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=100), nullable=False),
        sa.Column("external_id", sa.String(length=500), nullable=False),
        sa.Column("revision", sa.String(length=500), nullable=False),
        sa.Column("source_uri", sa.String(length=2000), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "external_id", "revision"),
    )
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("vault_path", sa.String(length=2000), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("document_type", sa.String(length=100), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_status", sa.String(length=100), nullable=False),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["source_record_id"], ["source_records.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("sha256"),
    )
    op.create_index("ix_documents_source_record_id", "documents", ["source_record_id"])
    op.create_table(
        "document_pages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=True),
        sa.Column("extraction_method", sa.String(length=100), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("document_id", "page_number"),
    )
    op.create_index("ix_document_pages_document_id", "document_pages", ["document_id"])
    op.create_table(
        "lab_observations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("canonical_name", sa.String(length=255), nullable=False),
        sa.Column("source_name", sa.String(length=500), nullable=False),
        sa.Column("source_value", sa.String(length=255), nullable=False),
        sa.Column("source_unit", sa.String(length=100), nullable=True),
        sa.Column("normalized_value", sa.Numeric(), nullable=True),
        sa.Column("normalized_unit", sa.String(length=100), nullable=True),
        sa.Column("reference_low", sa.Numeric(), nullable=True),
        sa.Column("reference_high", sa.Numeric(), nullable=True),
        sa.Column("reference_text", sa.String(length=500), nullable=True),
        sa.Column("evidence_excerpt", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=3, scale=2), nullable=False),
        sa.Column("status", review_status, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_lab_observations_document_id", "lab_observations", ["document_id"])
    op.create_index("ix_lab_observations_canonical_name", "lab_observations", ["canonical_name"])
    op.create_index("ix_lab_observations_status", "lab_observations", ["status"])
    op.create_table(
        "review_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=False),
        sa.Column("decision", sa.String(length=100), nullable=True),
        sa.Column("correction_json", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["observation_id"], ["lab_observations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("observation_id"),
    )
    op.create_index("ix_review_items_observation_id", "review_items", ["observation_id"])
    op.execute(
        "CREATE VIEW verified_lab_history AS "
        "SELECT * FROM lab_observations WHERE status = 'verified'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW verified_lab_history")
    op.drop_index("ix_review_items_observation_id", table_name="review_items")
    op.drop_table("review_items")
    op.drop_index("ix_lab_observations_status", table_name="lab_observations")
    op.drop_index("ix_lab_observations_canonical_name", table_name="lab_observations")
    op.drop_index("ix_lab_observations_document_id", table_name="lab_observations")
    op.drop_table("lab_observations")
    op.drop_index("ix_document_pages_document_id", table_name="document_pages")
    op.drop_table("document_pages")
    op.drop_index("ix_documents_source_record_id", table_name="documents")
    op.drop_table("documents")
    op.drop_table("source_records")
    review_status.drop(op.get_bind(), checkfirst=True)
