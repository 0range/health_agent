"""Add profile-scoped Google Sheets sync and decision audit.

Revision ID: 0006_google_sheets
Revises: 0005_whoop
Create Date: 2026-09-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_google_sheets"
down_revision: str | Sequence[str] | None = "0005_whoop"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sheets_sync_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decisions_applied", sa.Integer(), nullable=False),
        sa.Column("decisions_replayed", sa.Integer(), nullable=False),
        sa.Column("lab_rows", sa.Integer(), nullable=False),
        sa.Column("review_rows", sa.Integer(), nullable=False),
        sa.Column("source_rows", sa.Integer(), nullable=False),
        sa.Column("safe_error_code", sa.String(length=100), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_sheets_sync_runs_status",
        ),
        sa.ForeignKeyConstraint(["profile_id"], ["profiles.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sheets_sync_runs_profile_id", "sheets_sync_runs", ["profile_id"]
    )
    op.create_table(
        "sheets_review_decision_audits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("review_item_id", sa.Uuid(), nullable=False),
        sa.Column("observation_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("spreadsheet_id", sa.String(length=300), nullable=False),
        sa.Column("sheet_row", sa.Integer(), nullable=False),
        sa.Column("row_version", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("decision_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "correction_json",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('approve', 'correct', 'reject')",
            name="ck_sheets_review_decision_action",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id", "document_id"],
            ["lab_observations.id", "lab_observations.document_id"],
            name="fk_sheets_review_audit_observation_document",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
            name="fk_sheets_review_audit_document_profile",
        ),
        sa.ForeignKeyConstraint(["review_item_id"], ["review_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "profile_id", "review_item_id", name="uq_sheets_review_decision_once"
        ),
    )
    op.create_index(
        "ix_sheets_review_decision_audits_profile_id",
        "sheets_review_decision_audits",
        ["profile_id"],
    )
    op.create_index(
        "ix_sheets_review_decision_audits_review_item_id",
        "sheets_review_decision_audits",
        ["review_item_id"],
    )
    op.create_index(
        "ix_sheets_review_decision_audits_observation_id",
        "sheets_review_decision_audits",
        ["observation_id"],
    )
    op.create_index(
        "ix_sheets_review_decision_audits_document_id",
        "sheets_review_decision_audits",
        ["document_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_sheets_review_decision_audits_document_id",
        table_name="sheets_review_decision_audits",
    )
    op.drop_index(
        "ix_sheets_review_decision_audits_observation_id",
        table_name="sheets_review_decision_audits",
    )
    op.drop_index(
        "ix_sheets_review_decision_audits_review_item_id",
        table_name="sheets_review_decision_audits",
    )
    op.drop_index(
        "ix_sheets_review_decision_audits_profile_id",
        table_name="sheets_review_decision_audits",
    )
    op.drop_table("sheets_review_decision_audits")
    op.drop_index("ix_sheets_sync_runs_profile_id", table_name="sheets_sync_runs")
    op.drop_table("sheets_sync_runs")
