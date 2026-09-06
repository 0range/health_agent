"""Persistent visit records and immutable public snapshots."""

from dataclasses import dataclass
from datetime import datetime
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

from health_agent.models import Base


class HealthVisit(Base):
    __tablename__ = "health_visits"
    __table_args__ = (
        UniqueConstraint("id", "profile_id", name="uq_health_visits_id_profile"),
        UniqueConstraint("public_code", name="uq_health_visits_public_code"),
        UniqueConstraint("creation_key", name="uq_health_visits_creation_key"),
        ForeignKeyConstraint(
            ["source_document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
            name="fk_health_visits_document_profile",
        ),
        CheckConstraint(
            "status IN ('planned','completed','cancelled')",
            name="ck_health_visits_status",
        ),
        CheckConstraint("ends_at > starts_at", name="ck_health_visits_interval"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    public_code: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone_name: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="planned")
    source_document_id: Mapped[UUID | None]
    creation_key: Mapped[str] = mapped_column(String(200))
    creation_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class HealthVisitNote(Base):
    __tablename__ = "health_visit_notes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["visit_id", "profile_id"],
            ["health_visits.id", "health_visits.profile_id"],
            name="fk_health_visit_notes_visit_profile",
        ),
        UniqueConstraint("action_key", name="uq_health_visit_notes_action_key"),
        CheckConstraint(
            "kind IN ('question','answer')", name="ck_health_visit_notes_kind"
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    visit_id: Mapped[UUID] = mapped_column(index=True)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(String(10000))
    action_key: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


@dataclass(frozen=True, slots=True)
class Visit:
    id: UUID
    profile_id: UUID
    public_code: str
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone_name: str
    status: str
    source_document_id: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class VisitNote:
    id: UUID
    visit_id: UUID
    profile_id: UUID
    kind: str
    text: str
    created_at: datetime
