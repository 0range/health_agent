"""Explicit visit publication with durable retries and post-commit network calls."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    String,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Mapped, Session, mapped_column

from health_agent.automation.storage import GlobalRunLock
from health_agent.db import session_scope
from health_agent.google_calendar.models import CalendarEvent, CalendarResult
from health_agent.models import Base, Profile
from health_agent.visits.models import HealthVisit, HealthVisitNote
from health_agent.visits.repository import VisitNotFound, VisitRepository


class VisitCalendarPublication(Base):
    __tablename__ = "visit_calendar_publications"
    __table_args__ = (
        ForeignKeyConstraint(
            ["visit_id", "profile_id"],
            ["health_visits.id", "health_visits.profile_id"],
            name="fk_visit_calendar_owner",
        ),
        CheckConstraint(
            "status IN ('queued','published')", name="ck_visit_calendar_status"
        ),
    )
    visit_id: Mapped[UUID] = mapped_column(primary_key=True)
    profile_id: Mapped[UUID] = mapped_column(index=True)
    target_subject: Mapped[str | None] = mapped_column(String(500))
    target_calendar: Mapped[str | None] = mapped_column(String(1024))
    successful_fingerprint: Mapped[str | None] = mapped_column(String(64))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default="queued")
    safe_error: Mapped[str | None] = mapped_column(String(64))
    html_link: Mapped[str | None] = mapped_column(String(2048))


@dataclass(frozen=True, slots=True)
class PublicationResult:
    status: str
    safe_error: str | None = None
    html_link: str | None = None


@dataclass(frozen=True, slots=True)
class PublicationSnapshot:
    opted_in: bool
    status: str
    safe_error: str | None = None
    html_link: str | None = None


_SAFE_ERRORS = frozenset(
    {
        "authorization_missing",
        "account_mismatch",
        "oauth_required",
        "reauth_required",
        "invalid_oauth_scopes",
        "permission_denied",
        "rate_limited",
        "google_unavailable",
        "calendar_request_failed",
        "remote_ownership_mismatch",
        "write_outcome_unknown",
        "calendar_configuration_invalid",
        "calendar_target_mismatch",
        "calendar_sync_failed",
        "publication_busy",
        "content_changed",
    }
)


def safe_calendar_link(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 2048
        or any(ord(c) < 32 for c in value)
    ):
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme == "https"
            and url.hostname in {"www.google.com", "calendar.google.com"}
            and url.port in (None, 443)
            and url.username is None
            and url.password is None
            and url.path.startswith("/calendar/")
        ):
            return value
    except ValueError:
        pass
    return None


def publication_notice(result: PublicationResult) -> str:
    if result.status == "queued":
        return "Calendar: публикация в очереди; локальные изменения сохранены."
    if result.status == "published":
        return "Calendar: изменения подтверждены."
    return "Calendar: без изменений." if result.html_link else ""


def _event(session: Session, profile_id: UUID, code: str) -> CalendarEvent:
    visit = VisitRepository(session).get(profile_id, code)
    notes = session.scalars(
        select(HealthVisitNote.text)
        .where(
            HealthVisitNote.profile_id == profile_id,
            HealthVisitNote.visit_id == visit.id,
            HealthVisitNote.kind == "question",
        )
        .order_by(HealthVisitNote.created_at, HealthVisitNote.id)
        .limit(21)
    ).all()
    marker = " [Вопрос сокращён.]"
    questions = [
        q if len(q) <= 1000 else q[: 1000 - len(marker)] + marker for q in notes[:20]
    ]
    if len(notes) > 20:
        marker = " [Показаны первые 20 вопросов.]"
        questions[-1] = questions[-1][: 1000 - len(marker)] + marker
    return CalendarEvent(
        profile_id,
        visit.id,
        visit.title,
        visit.starts_at,
        visit.ends_at,
        visit.timezone_name,
        tuple(questions),
        visit.status == "cancelled",
    )


def _fingerprint(event: CalendarEvent) -> str:
    return hashlib.sha256(
        json.dumps(
            asdict(event), default=str, ensure_ascii=False, sort_keys=True
        ).encode()
    ).hexdigest()


class CalendarPublicationService:
    def __init__(self, engine, calendar_service, lock_root: Path):
        self.engine, self.calendar, self.lock_root = (
            engine,
            calendar_service,
            Path(lock_root),
        )

    def publish(self, profile_id: UUID, code: str) -> PublicationResult:
        return self.sync_visit(profile_id, code, only_if_opted_in=False)

    def sync_visit(
        self, profile_id: UUID, code: str, *, only_if_opted_in: bool = True
    ) -> PublicationResult:
        # Resolve ownership before any opt-in, filesystem write or connector access.
        with session_scope(self.engine) as session:
            visit = VisitRepository(session).get(profile_id, code)
            if (
                only_if_opted_in
                and session.get(VisitCalendarPublication, visit.id) is None
            ):
                return PublicationResult("unchanged")
        lock = GlobalRunLock(self.lock_root / str(profile_id) / f"{visit.id}.lock")
        if not lock.acquire():
            return PublicationResult("queued", "publication_busy")
        target_lock = None
        try:
            with session_scope(self.engine) as session:
                event = _event(session, profile_id, code)
                session.execute(
                    insert(VisitCalendarPublication)
                    .values(visit_id=visit.id, profile_id=profile_id, status="queued")
                    .on_conflict_do_nothing(
                        index_elements=[VisitCalendarPublication.visit_id]
                    )
                )
            sent_fingerprint = _fingerprint(event)
            # Share the profile store's lock: configure/authorize cannot retarget
            # an in-flight publication between binding and CalendarService's read.
            target_lock = GlobalRunLock(self.calendar.profiles.root / "publish.lock")
            if not target_lock.acquire():
                return self._record(
                    profile_id,
                    code,
                    event,
                    sent_fingerprint,
                    CalendarResult("", "deferred", safe_error="publication_busy"),
                )
            try:
                target = self.calendar.profiles.load(profile_id)
                if not target.enabled or not target.account_subject:
                    raise FileNotFoundError
            except (FileNotFoundError, ValueError, TypeError, RuntimeError, OSError):
                return self._record(
                    profile_id,
                    code,
                    event,
                    sent_fingerprint,
                    CalendarResult("", "deferred", safe_error="authorization_missing"),
                )
            with session_scope(self.engine) as session:
                row = session.get(VisitCalendarPublication, visit.id)
                assert row is not None and row.profile_id == profile_id
                if row.target_subject is not None and (
                    row.target_subject,
                    row.target_calendar,
                ) != (target.account_subject, target.calendar_id):
                    row.status, row.safe_error = "queued", "calendar_target_mismatch"
                    row.attempted_at = datetime.now(UTC)
                    return PublicationResult("queued", row.safe_error)
                row.target_subject, row.target_calendar = (
                    target.account_subject,
                    target.calendar_id,
                )
                row.attempted_at = datetime.now(UTC)
                if (
                    row.status == "published"
                    and row.successful_fingerprint == sent_fingerprint
                ):
                    return PublicationResult(
                        "unchanged", html_link=safe_calendar_link(row.html_link)
                    )
            # No SQL transaction survives this boundary. The exact sent event is immutable.
            try:
                result = self.calendar.sync(event)
            except (TimeoutError, ConnectionError):
                result = CalendarResult(
                    "", "deferred", safe_error="write_outcome_unknown"
                )
            except Exception:  # noqa: BLE001 - external errors never expose content.
                result = CalendarResult(
                    "", "deferred", safe_error="calendar_sync_failed"
                )
            return self._record(profile_id, code, event, sent_fingerprint, result)
        finally:
            if target_lock is not None:
                target_lock.release()
            lock.release()

    def _record(
        self, profile_id, code, event, fingerprint, result
    ) -> PublicationResult:
        success = result.status in {"created", "updated", "unchanged", "cancelled"}
        error = (
            result.safe_error
            if result.safe_error in _SAFE_ERRORS
            else "calendar_sync_failed"
        )
        link = safe_calendar_link(result.html_link)
        with session_scope(self.engine) as session:
            row = session.get(VisitCalendarPublication, event.visit_id)
            assert row is not None and row.profile_id == profile_id
            row.attempted_at = datetime.now(UTC)
            if success:
                row.successful_fingerprint, row.synced_at = (
                    fingerprint,
                    datetime.now(UTC),
                )
                row.html_link = link or row.html_link
                changed = _fingerprint(_event(session, profile_id, code)) != fingerprint
                row.status, row.safe_error = (
                    ("queued", "content_changed") if changed else ("published", None)
                )
            else:
                row.status, row.safe_error = "queued", error
            return PublicationResult(
                row.status, row.safe_error, safe_calendar_link(row.html_link)
            )

    def sync_profile(
        self, profile_id: UUID, limit: int = 100
    ) -> tuple[PublicationResult, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid_calendar_limit")
        with session_scope(self.engine) as session:
            if session.get(Profile, profile_id) is None:
                raise VisitNotFound("visit_not_found")
            codes = session.scalars(
                select(HealthVisit.public_code)
                .join(
                    VisitCalendarPublication,
                    (VisitCalendarPublication.visit_id == HealthVisit.id)
                    & (VisitCalendarPublication.profile_id == HealthVisit.profile_id),
                )
                .where(HealthVisit.profile_id == profile_id)
                .order_by(
                    VisitCalendarPublication.attempted_at.asc().nulls_first(),
                    HealthVisit.id,
                )
                .limit(limit)
            ).all()
        return tuple(self.sync_visit(profile_id, code) for code in codes)

    def snapshot(self, profile_id: UUID, code: str) -> PublicationSnapshot:
        with session_scope(self.engine) as session:
            event = _event(session, profile_id, code)
            row = session.get(VisitCalendarPublication, event.visit_id)
            if row is None:
                return PublicationSnapshot(False, "local_only")
            status = (
                row.status
                if row.successful_fingerprint == _fingerprint(event)
                else "queued"
            )
            return PublicationSnapshot(
                True, status, row.safe_error, safe_calendar_link(row.html_link)
            )

    def backlog(self, profile_id: UUID) -> int:
        with session_scope(self.engine) as session:
            return int(
                session.scalar(
                    select(func.count())
                    .select_from(VisitCalendarPublication)
                    .where(
                        VisitCalendarPublication.profile_id == profile_id,
                        VisitCalendarPublication.status == "queued",
                    )
                )
                or 0
            )

    def last_success_at(self, profile_id: UUID) -> datetime | None:
        with session_scope(self.engine) as session:
            return session.scalar(
                select(func.max(VisitCalendarPublication.synced_at)).where(
                    VisitCalendarPublication.profile_id == profile_id
                )
            )
