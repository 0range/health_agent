"""Database audit records for Google Sheets synchronization."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from health_agent.models import Base, utc_now


class SheetsSyncRun(Base):
    __tablename__ = "sheets_sync_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_sheets_sync_runs_status",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    status: Mapped[str] = mapped_column(String(32), default="running")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decisions_applied: Mapped[int] = mapped_column(Integer, default=0)
    decisions_replayed: Mapped[int] = mapped_column(Integer, default=0)
    lab_rows: Mapped[int] = mapped_column(Integer, default=0)
    review_rows: Mapped[int] = mapped_column(Integer, default=0)
    source_rows: Mapped[int] = mapped_column(Integer, default=0)
    safe_error_code: Mapped[str | None] = mapped_column(String(100))


class SheetsReviewDecisionAudit(Base):
    __tablename__ = "sheets_review_decision_audits"
    __table_args__ = (
        CheckConstraint(
            "action IN ('approve', 'correct', 'reject')",
            name="ck_sheets_review_decision_action",
        ),
        CheckConstraint("sheet_row >= 2", name="ck_sheets_review_decision_sheet_row"),
        UniqueConstraint(
            "profile_id", "review_item_id", name="uq_sheets_review_decision_once"
        ),
        ForeignKeyConstraint(
            ["observation_id", "document_id"],
            ["lab_observations.id", "lab_observations.document_id"],
            name="fk_sheets_review_audit_observation_document",
        ),
        ForeignKeyConstraint(
            ["review_item_id", "observation_id"],
            ["review_items.id", "review_items.observation_id"],
            name="fk_sheets_review_audit_review_observation",
        ),
        ForeignKeyConstraint(
            ["document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
            name="fk_sheets_review_audit_document_profile",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    review_item_id: Mapped[UUID] = mapped_column(index=True)
    observation_id: Mapped[UUID] = mapped_column(index=True)
    document_id: Mapped[UUID] = mapped_column(index=True)
    spreadsheet_id: Mapped[str] = mapped_column(String(300))
    sheet_row: Mapped[int] = mapped_column(Integer)
    row_version: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(16))
    decision_hash: Mapped[str] = mapped_column(String(64))
    correction_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now
    )
