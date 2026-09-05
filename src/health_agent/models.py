from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class ReviewStatus(StrEnum):
    VERIFIED = "verified"
    NEEDS_REVIEW = "needs_review"
    REJECTED = "rejected"


DEFAULT_PROFILE_ID = UUID("00000000-0000-0000-0000-000000000001")


class Profile(Base):
    __tablename__ = "profiles"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    documents: Mapped[list[Document]] = relationship(back_populates="profile")
    source_records: Mapped[list[SourceRecord]] = relationship(back_populates="profile")


class SourceRecord(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        UniqueConstraint("profile_id", "provider", "external_id", "revision"),
        UniqueConstraint("id", "profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(String(500))
    revision: Mapped[str] = mapped_column(String(500))
    source_uri: Mapped[str | None] = mapped_column(String(2000))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    profile: Mapped[Profile] = relationship(back_populates="source_records")
    document_links: Mapped[list[DocumentSourceRecord]] = relationship(
        back_populates="source_record", viewonly=True
    )


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("profile_id", "sha256"),
        UniqueConstraint("id", "profile_id"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    sha256: Mapped[str] = mapped_column(String(64))
    vault_path: Mapped[str] = mapped_column(String(2000))
    media_type: Mapped[str] = mapped_column(String(255))
    document_type: Mapped[str] = mapped_column(String(100))
    issued_date: Mapped[date | None]
    collected_date: Mapped[date | None]
    processing_status: Mapped[str] = mapped_column(String(100), default="pending")
    safe_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    profile: Mapped[Profile] = relationship(back_populates="documents")
    source_links: Mapped[list[DocumentSourceRecord]] = relationship(
        back_populates="document", viewonly=True
    )
    pages: Mapped[list[DocumentPage]] = relationship(back_populates="document")
    observations: Mapped[list[LabObservation]] = relationship(back_populates="document")


class DocumentSourceRecord(Base):
    """One immutable document's occurrence in one external/local source."""

    __tablename__ = "document_source_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
        ),
        ForeignKeyConstraint(
            ["source_record_id", "profile_id"],
            ["source_records.id", "source_records.profile_id"],
        ),
    )

    document_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_record_id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped[Document] = relationship(
        back_populates="source_links", viewonly=True
    )
    source_record: Mapped[SourceRecord] = relationship(
        back_populates="document_links", viewonly=True
    )


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (
        UniqueConstraint("document_id", "page_number"),
        CheckConstraint("page_number >= 1", name="ck_document_pages_page_number_positive"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    page_number: Mapped[int]
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extraction_method: Mapped[str] = mapped_column(String(100))

    document: Mapped[Document] = relationship(back_populates="pages")


class LabObservation(Base):
    __tablename__ = "lab_observations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["document_id", "page_number"],
            ["document_pages.document_id", "document_pages.page_number"],
        ),
        ForeignKeyConstraint(
            ["supersedes_observation_id", "document_id"],
            ["lab_observations.id", "lab_observations.document_id"],
            name="fk_lab_observations_supersedes_same_document",
        ),
        UniqueConstraint("id", "document_id"),
        CheckConstraint("page_number >= 1", name="ck_lab_observations_page_number_positive"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_lab_observations_confidence_range"
        ),
        CheckConstraint(
            "reference_low IS NULL OR reference_high IS NULL OR reference_low <= reference_high",
            name="ck_lab_observations_reference_range",
        ),
        CheckConstraint(
            "status <> 'verified' OR "
            "(parsed_value IS NOT NULL AND normalized_value IS NOT NULL "
            "AND normalized_unit IS NOT NULL)",
            name="ck_verified_observations_normalized",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.id"), index=True)
    supersedes_observation_id: Mapped[UUID | None] = mapped_column(index=True)
    page_number: Mapped[int]
    canonical_name: Mapped[str] = mapped_column(String(255), index=True)
    source_name: Mapped[str] = mapped_column(String(500))
    source_value: Mapped[str] = mapped_column(String(255))
    parsed_value: Mapped[Decimal | None] = mapped_column(Numeric)
    source_unit: Mapped[str | None] = mapped_column(String(100))
    normalized_value: Mapped[Decimal | None] = mapped_column(Numeric)
    normalized_unit: Mapped[str | None] = mapped_column(String(100))
    reference_low: Mapped[Decimal | None] = mapped_column(Numeric)
    reference_high: Mapped[Decimal | None] = mapped_column(Numeric)
    reference_text: Mapped[str | None] = mapped_column(String(500))
    evidence_excerpt: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Numeric(3, 2))
    status: Mapped[ReviewStatus] = mapped_column(
        Enum(
            ReviewStatus,
            name="review_status",
            values_callable=lambda statuses: [status.value for status in statuses],
            validate_strings=True,
        ),
        default=ReviewStatus.NEEDS_REVIEW,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    document: Mapped[Document] = relationship(back_populates="observations")
    review_item: Mapped[ReviewItem | None] = relationship(back_populates="observation", uselist=False)
    @property
    def is_publishable(self) -> bool:
        return self.status == ReviewStatus.VERIFIED


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (
        UniqueConstraint("observation_id"),
        UniqueConstraint("id", "observation_id", name="uq_review_items_id_observation"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    observation_id: Mapped[UUID] = mapped_column(
        ForeignKey("lab_observations.id"), index=True
    )
    reason_code: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str | None] = mapped_column(String(100))
    correction_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    observation: Mapped[LabObservation] = relationship(back_populates="review_item")
