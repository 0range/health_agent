"""Profile-scoped durable records for the three small daily-use coaches."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Engine, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB, insert
from sqlalchemy.orm import Mapped, mapped_column

from health_agent.db import session_scope
from health_agent.models import Base
from health_agent.pilot.contracts import Record


class PilotRecord(Base):
    __tablename__ = "pilot_records"
    __table_args__ = (UniqueConstraint("profile_id", "domain", "kind", "source_key"),)

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(ForeignKey("profiles.id"), index=True)
    domain: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(64))
    source_key: Mapped[str] = mapped_column(String(500))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB)


def _record(row: PilotRecord) -> Record:
    return Record(
        str(row.id), row.domain, row.kind, row.source_key, row.at, row.payload
    )


class PilotStore:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def put(
        self,
        profile_id: UUID,
        domain: str,
        kind: str,
        source_key: str,
        payload: dict[str, Any],
        *,
        at: datetime | None = None,
    ) -> Record:
        observed = at or datetime.now(UTC)
        if observed.tzinfo is None or not source_key.strip() or not kind.strip():
            raise ValueError("invalid_pilot_record")
        if domain not in {"sleep", "food", "training", "shared"}:
            raise ValueError("invalid_pilot_domain")
        with session_scope(self.engine) as session:
            session.execute(
                insert(PilotRecord)
                .values(
                    id=uuid4(),
                    profile_id=profile_id,
                    domain=domain,
                    kind=kind,
                    source_key=source_key,
                    at=observed,
                    payload=payload,
                )
                .on_conflict_do_nothing(
                    index_elements=["profile_id", "domain", "kind", "source_key"]
                )
            )
            row = session.scalar(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.domain == domain,
                    PilotRecord.kind == kind,
                    PilotRecord.source_key == source_key,
                )
            )
            assert row is not None
            return _record(row)

    def list(
        self,
        profile_id: UUID,
        domain: str,
        kind: str | None = None,
        *,
        limit: int = 100,
    ) -> list[Record]:
        statement = select(PilotRecord).where(
            PilotRecord.profile_id == profile_id,
            PilotRecord.domain == domain,
        )
        if kind is not None:
            statement = statement.where(PilotRecord.kind == kind)
        statement = statement.order_by(PilotRecord.at.desc(), PilotRecord.id.desc())
        with session_scope(self.engine) as session:
            return [
                _record(row)
                for row in session.scalars(statement.limit(max(0, min(limit, 1000))))
            ]

    def by_source(
        self, profile_id: UUID, domain: str, kind: str, source_key: str,
    ) -> Record | None:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.domain == domain,
                    PilotRecord.kind == kind,
                    PilotRecord.source_key == source_key,
                )
            )
            return None if row is None else _record(row)

    def get(self, profile_id: UUID, record_id: str) -> Record | None:
        try:
            identifier = UUID(record_id)
        except ValueError:
            return None
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.id == identifier,
                )
            )
            return None if row is None else _record(row)

    def patch(
        self, profile_id: UUID, record_id: str, payload: dict[str, Any]
    ) -> Record:
        with session_scope(self.engine) as session:
            row = session.scalar(
                select(PilotRecord)
                .where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.id == UUID(record_id),
                )
                .with_for_update()
            )
            if row is None:
                raise ValueError("pilot_record_not_found")
            row.payload = payload
            session.flush()
            return _record(row)
