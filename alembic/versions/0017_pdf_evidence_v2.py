"""Allow versioned evidence for additional source-proven PDF table layouts."""

from alembic import op

revision = "0017_pdf_evidence_v2"
down_revision = "0016_extraction_backfill_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_page_evidence_method", "page_evidence", type_="check")
    op.create_check_constraint(
        "ck_page_evidence_method",
        "page_evidence",
        "method IN ('pdf_table_v1', 'pdf_table_v2')",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE page_evidence IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM page_evidence WHERE method = 'pdf_table_v2') THEN
                RAISE EXCEPTION 'Refusing to downgrade existing pdf_table_v2 evidence';
            END IF;
        END $$;
    """)
    op.drop_constraint("ck_page_evidence_method", "page_evidence", type_="check")
    op.create_check_constraint(
        "ck_page_evidence_method",
        "page_evidence",
        "method IN ('pdf_table_v1')",
    )
