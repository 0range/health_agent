"""Validated, transactional visit workflow; notes are append-only."""

import hashlib
import json
import re
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import case, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from health_agent.models import Document, Profile
from health_agent.reminders.time import require_aware_utc, validate_timezone
from health_agent.visits.models import HealthVisit, HealthVisitNote, Visit, VisitNote

_CODE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{5,31}$")


class VisitNotFound(LookupError):
    """The requested visit or source does not belong to this profile."""


def _bounded(value: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError("invalid_visit_input")
    return value.strip()


def _limit(value: int) -> int:
    if type(value) is not int or not 1 <= value <= 100:
        raise ValueError("invalid_visit_limit")
    return value


def _interval(
    starts_at: datetime, ends_at: datetime, timezone_name: str
) -> tuple[datetime, datetime, str]:
    start, end = require_aware_utc(starts_at), require_aware_utc(ends_at)
    zone = validate_timezone(_bounded(timezone_name, 100)).key
    if end <= start:
        raise ValueError("invalid_visit_interval")
    return start, end, zone


class VisitRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        profile_id: UUID,
        *,
        title: str,
        starts_at: datetime,
        ends_at: datetime,
        timezone_name: str,
        creation_key: str,
        source_document_id: UUID | None = None,
    ) -> Visit:
        title, key = _bounded(title, 200), _bounded(creation_key, 200)
        start, end, zone = _interval(starts_at, ends_at, timezone_name)
        fingerprint = hashlib.sha256(
            json.dumps(
                [
                    str(profile_id),
                    title,
                    start.isoformat(),
                    end.isoformat(),
                    zone,
                    str(source_document_id) if source_document_id else None,
                ],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        if self.session.get(Profile, profile_id) is None:
            raise VisitNotFound("visit_not_found")
        if (
            source_document_id is not None
            and self.session.scalar(
                select(Document.id).where(
                    Document.id == source_document_id, Document.profile_id == profile_id
                )
            )
            is None
        ):
            raise VisitNotFound("visit_not_found")
        self.session.execute(
            insert(HealthVisit)
            .values(
                profile_id=profile_id,
                public_code=f"v{secrets.token_urlsafe(9)}",
                title=title,
                starts_at=start,
                ends_at=end,
                timezone_name=zone,
                status="planned",
                source_document_id=source_document_id,
                creation_key=key,
                creation_fingerprint=fingerprint,
            )
            .on_conflict_do_nothing(index_elements=[HealthVisit.creation_key])
        )
        row = self.session.scalar(
            select(HealthVisit).where(HealthVisit.creation_key == key)
        )
        assert row is not None
        if row.profile_id != profile_id or row.creation_fingerprint != fingerprint:
            raise ValueError("visit_action_conflict")
        return _snapshot(row)

    def _row(self, profile_id: UUID, code: str, *, lock: bool = False) -> HealthVisit:
        if not isinstance(code, str) or _CODE.fullmatch(code) is None:
            raise VisitNotFound("visit_not_found")
        statement = select(HealthVisit).where(
            HealthVisit.profile_id == profile_id, HealthVisit.public_code == code
        )
        if lock:
            statement = statement.with_for_update().execution_options(
                populate_existing=True
            )
        row = self.session.scalar(statement)
        if row is None:
            raise VisitNotFound("visit_not_found")
        return row

    def get(self, profile_id: UUID, code: str) -> Visit:
        return _snapshot(self._row(profile_id, code))

    def list(self, profile_id: UUID, limit: int = 20) -> tuple[Visit, ...]:
        now = datetime.now(UTC)
        rows = self.session.scalars(
            select(HealthVisit)
            .where(HealthVisit.profile_id == profile_id)
            .order_by(
                case(
                    (
                        (HealthVisit.status == "planned")
                        & (HealthVisit.starts_at >= now),
                        0,
                    ),
                    else_=1,
                ),
                HealthVisit.starts_at,
                HealthVisit.id,
            )
            .limit(_limit(limit))
        )
        return tuple(_snapshot(row) for row in rows)

    def add_note(
        self, profile_id: UUID, code: str, *, kind: str, text: str, action_key: str
    ) -> VisitNote:
        value, key = _bounded(text, 10000), _bounded(action_key, 200)
        if kind not in {"question", "answer"}:
            raise ValueError("invalid_visit_note_kind")
        row = self._row(profile_id, code, lock=True)
        existing = self.session.scalar(
            select(HealthVisitNote).where(HealthVisitNote.action_key == key)
        )
        if existing is None:
            if row.status == "cancelled":
                raise ValueError("visit_transition_not_allowed")
            self.session.execute(
                insert(HealthVisitNote)
                .values(
                    visit_id=row.id,
                    profile_id=profile_id,
                    kind=kind,
                    text=value,
                    action_key=key,
                )
                .on_conflict_do_nothing(index_elements=[HealthVisitNote.action_key])
            )
            existing = self.session.scalar(
                select(HealthVisitNote).where(HealthVisitNote.action_key == key)
            )
        assert existing is not None
        if (existing.profile_id, existing.visit_id, existing.kind, existing.text) != (
            profile_id,
            row.id,
            kind,
            value,
        ):
            raise ValueError("visit_action_conflict")
        return _note(existing)

    def notes(
        self, profile_id: UUID, code: str, limit: int = 100
    ) -> tuple[VisitNote, ...]:
        count = _limit(limit)
        row = self._row(profile_id, code)
        rows = self.session.scalars(
            select(HealthVisitNote)
            .where(
                HealthVisitNote.visit_id == row.id,
                HealthVisitNote.profile_id == profile_id,
            )
            .order_by(HealthVisitNote.created_at, HealthVisitNote.id)
            .limit(count)
        )
        return tuple(_note(note) for note in rows)

    def complete(self, profile_id: UUID, code: str) -> Visit:
        return self._terminal(profile_id, code, "completed")

    def cancel(self, profile_id: UUID, code: str) -> Visit:
        return self._terminal(profile_id, code, "cancelled")

    def _terminal(self, profile_id: UUID, code: str, status: str) -> Visit:
        row = self._row(profile_id, code, lock=True)
        if row.status == status:
            return _snapshot(row)
        if row.status != "planned":
            raise ValueError("visit_transition_not_allowed")
        row.status, row.updated_at = status, datetime.now(UTC)
        self.session.flush()
        return _snapshot(row)

    def reschedule(
        self,
        profile_id: UUID,
        code: str,
        *,
        starts_at: datetime,
        ends_at: datetime,
        timezone_name: str,
    ) -> Visit:
        start, end, zone = _interval(starts_at, ends_at, timezone_name)
        row = self._row(profile_id, code, lock=True)
        if row.status != "planned":
            raise ValueError("visit_transition_not_allowed")
        row.starts_at, row.ends_at, row.timezone_name = start, end, zone
        row.updated_at = datetime.now(UTC)
        self.session.flush()
        return _snapshot(row)


def _snapshot(row: HealthVisit) -> Visit:
    return Visit(
        row.id,
        row.profile_id,
        row.public_code,
        row.title,
        require_aware_utc(row.starts_at),
        require_aware_utc(row.ends_at),
        row.timezone_name,
        row.status,
        row.source_document_id,
        require_aware_utc(row.created_at),
        require_aware_utc(row.updated_at),
    )


def _note(row: HealthVisitNote) -> VisitNote:
    return VisitNote(
        row.id,
        row.visit_id,
        row.profile_id,
        row.kind,
        row.text,
        require_aware_utc(row.created_at),
    )
