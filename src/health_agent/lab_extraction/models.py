"""Durable per-profile budget and versioned page queue; no medical text copies."""

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from health_agent.lab_extraction.types import EXTRACTOR_VERSION
from health_agent.models import Base


class LabExtractionProfile(Base):
    __tablename__ = "lab_extraction_profiles"
    __table_args__ = (
        CheckConstraint(
            "daily_budget BETWEEN 1 AND 100", name="ck_extraction_daily_budget"
        ),
        CheckConstraint(
            "cloud_requests_today >= 0", name="ck_extraction_daily_requests"
        ),
    )
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("profiles.id"), primary_key=True
    )
    enabled: Mapped[bool] = mapped_column(default=True)
    cloud_enabled: Mapped[bool] = mapped_column(default=False)
    daily_budget: Mapped[int] = mapped_column(default=20)
    cloud_day: Mapped[date | None]
    cloud_requests_today: Mapped[int] = mapped_column(default=0)


class LabExtractionJob(Base):
    __tablename__ = "lab_extraction_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "profile_id"], ["documents.id", "documents.profile_id"]
        ),
        ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
        ),
        UniqueConstraint(
            "document_id",
            "page_number",
            "extractor_version",
            name="uq_extraction_page_version",
        ),
        CheckConstraint("page_number >= 1", name="ck_extraction_page"),
        CheckConstraint(
            "local_attempts >= 0 AND cloud_attempts BETWEEN 0 AND 3 AND candidate_count BETWEEN 0 AND 40",
            name="ck_extraction_counters",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_cloud','cloud_in_flight','completed','needs_attention')",
            name="ck_extraction_status",
        ),
        CheckConstraint(
            "(status IN ('running','cloud_in_flight') AND claim_token IS NOT NULL) OR (status NOT IN ('running','cloud_in_flight') AND claim_token IS NULL)",
            name="ck_extraction_claim",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("lab_extraction_profiles.profile_id"), index=True
    )
    document_id: Mapped[UUID] = mapped_column(index=True)
    page_number: Mapped[int]
    extractor_version: Mapped[str] = mapped_column(
        String(100), default=EXTRACTOR_VERSION
    )
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    claim_token: Mapped[UUID | None]
    local_completed: Mapped[bool] = mapped_column(default=False)
    local_attempts: Mapped[int] = mapped_column(default=0)
    cloud_attempts: Mapped[int] = mapped_column(default=0)
    candidate_count: Mapped[int] = mapped_column(default=0)
    source_text_sha256: Mapped[str | None] = mapped_column(String(64))
    extraction_method: Mapped[str | None] = mapped_column(String(100))
    model_name: Mapped[str | None] = mapped_column(String(255))
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
