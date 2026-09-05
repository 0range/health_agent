"""Durable bounded lab extraction queue, profile opt-in and printed flags."""

import sqlalchemy as sa

from alembic import op

revision = "0008_lab_extraction"
down_revision = "0007_google_sheets"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lab_observations", sa.Column("source_flag", sa.String(8), nullable=True)
    )
    op.create_check_constraint(
        "ck_lab_observations_source_flag",
        "lab_observations",
        "source_flag IS NULL OR source_flag IN ('H','L','↑','↓','*')",
    )
    op.create_table(
        "lab_extraction_profiles",
        sa.Column(
            "profile_id", sa.Uuid(), sa.ForeignKey("profiles.id"), primary_key=True
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("cloud_enabled", sa.Boolean(), nullable=False),
        sa.Column("daily_budget", sa.Integer(), nullable=False),
        sa.Column("cloud_day", sa.Date(), nullable=True),
        sa.Column("cloud_requests_today", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "daily_budget BETWEEN 1 AND 100", name="ck_extraction_daily_budget"
        ),
        sa.CheckConstraint(
            "cloud_requests_today >= 0", name="ck_extraction_daily_requests"
        ),
    )
    op.create_table(
        "lab_extraction_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id",
            sa.Uuid(),
            sa.ForeignKey("lab_extraction_profiles.profile_id"),
            nullable=False,
        ),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=False),
        sa.Column("extractor_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("claim_token", sa.Uuid(), nullable=True),
        sa.Column("local_completed", sa.Boolean(), nullable=False),
        sa.Column("local_attempts", sa.Integer(), nullable=False),
        sa.Column("cloud_attempts", sa.Integer(), nullable=False),
        sa.Column("candidate_count", sa.Integer(), nullable=False),
        sa.Column("source_text_sha256", sa.String(64), nullable=True),
        sa.Column("extraction_method", sa.String(100), nullable=True),
        sa.Column("model_name", sa.String(255), nullable=True),
        sa.Column("safe_error_code", sa.String(100), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "profile_id"], ["documents.id", "documents.profile_id"]
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
        ),
        sa.UniqueConstraint(
            "document_id",
            "page_number",
            "extractor_version",
            name="uq_extraction_page_version",
        ),
        sa.CheckConstraint("page_number >= 1", name="ck_extraction_page"),
        sa.CheckConstraint(
            "local_attempts >= 0 AND cloud_attempts BETWEEN 0 AND 3 AND candidate_count >= 0",
            name="ck_extraction_counters",
        ),
        sa.CheckConstraint(
            "status IN ('queued','running','waiting_cloud','cloud_in_flight','completed','needs_attention')",
            name="ck_extraction_status",
        ),
        sa.CheckConstraint(
            "(status IN ('running','cloud_in_flight') AND claim_token IS NOT NULL) OR (status NOT IN ('running','cloud_in_flight') AND claim_token IS NULL)",
            name="ck_extraction_claim",
        ),
    )
    for name in ("profile_id", "document_id", "status"):
        op.create_index(f"ix_lab_extraction_jobs_{name}", "lab_extraction_jobs", [name])


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM lab_extraction_profiles)
          OR EXISTS (SELECT 1 FROM lab_extraction_jobs)
          OR EXISTS (SELECT 1 FROM lab_observations WHERE source_flag IS NOT NULL)
        THEN RAISE EXCEPTION 'Refusing to downgrade lab extraction with existing state';
        END IF;
    END $$""")
    op.drop_table("lab_extraction_jobs")
    op.drop_table("lab_extraction_profiles")
    op.drop_constraint(
        "ck_lab_observations_source_flag", "lab_observations", type_="check"
    )
    op.drop_column("lab_observations", "source_flag")
