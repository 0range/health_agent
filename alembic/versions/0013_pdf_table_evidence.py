"""Immutable source-proven PDF table evidence."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_pdf_table_evidence"
down_revision = "0011_medical_workflows"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "page_evidence",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("method", sa.String(100), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("evidence_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("id", "document_id", "page_number"),
        sa.UniqueConstraint("document_id", "page_number", "method", "source_sha256"),
        sa.CheckConstraint("page_number >= 1", name="ck_page_evidence_page_positive"),
        sa.CheckConstraint(
            "method IN ('pdf_table_v1')", name="ck_page_evidence_method"
        ),
    )
    op.create_index("ix_page_evidence_document_id", "page_evidence", ["document_id"])
    op.add_column(
        "lab_observations", sa.Column("page_evidence_id", sa.Uuid(), nullable=True)
    )
    op.create_index(
        "ix_lab_observations_page_evidence_id", "lab_observations", ["page_evidence_id"]
    )
    op.create_foreign_key(
        "fk_lab_observations_page_evidence",
        "lab_observations",
        "page_evidence",
        ["page_evidence_id", "document_id", "page_number"],
        ["id", "document_id", "page_number"],
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM page_evidence)
           OR EXISTS (SELECT 1 FROM lab_observations WHERE page_evidence_id IS NOT NULL)
        THEN RAISE EXCEPTION 'Refusing to erase immutable PDF evidence';
        END IF;
    END $$""")
    op.drop_constraint(
        "fk_lab_observations_page_evidence", "lab_observations", type_="foreignkey"
    )
    op.drop_index("ix_lab_observations_page_evidence_id", table_name="lab_observations")
    op.drop_column("lab_observations", "page_evidence_id")
    op.drop_index("ix_page_evidence_document_id", table_name="page_evidence")
    op.drop_table("page_evidence")
