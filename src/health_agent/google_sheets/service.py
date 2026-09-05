"""Locked, convergent Google Sheets synchronization orchestration."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.orm import Session

from health_agent.google_sheets.api import safe_sheets_error_code
from health_agent.google_sheets.config import WORKBOOK_SCHEMA_VERSION, SheetsProfile
from health_agent.google_sheets.decisions import (
    DecisionReport,
    apply_decisions,
    parse_decisions,
)
from health_agent.google_sheets.models import SheetsSyncRun
from health_agent.google_sheets.oauth import SheetsOAuth
from health_agent.google_sheets.projection import SourceStatusRow, build_projection
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsStateStore,
)
from health_agent.google_sheets.types import SheetsGateway, WorkbookBinding
from health_agent.models import Profile, utc_now


class WorkbookOwnershipError(RuntimeError):
    """The configured remote workbook is not owned by this profile binding."""


class SheetsSyncFailure(RuntimeError):
    def __init__(self, safe_code: str) -> None:
        super().__init__(safe_code)
        self.safe_code = safe_code


@dataclass(frozen=True, slots=True)
class SheetsSyncReport:
    status: str
    decisions_applied: int
    decisions_replayed: int
    lab_rows: int
    review_rows: int
    source_rows: int
    spreadsheet_id: str


@dataclass(frozen=True, slots=True)
class SheetsStatus:
    configured: bool
    authorization: str
    spreadsheet_configured: bool
    last_status: str | None
    last_success_at: str | None
    safe_error_code: str | None


class SheetsService:
    def __init__(
        self,
        profiles: LocalSheetsProfileStore,
        state: LocalSheetsStateStore,
        oauth: SheetsOAuth,
        gateway_factory: Callable[[object], SheetsGateway],
        sessions: Callable[[], AbstractContextManager[Session]],
        source_statuses: Callable[[Session, UUID], tuple[SourceStatusRow, ...]],
    ) -> None:
        self.profiles = profiles
        self.state = state
        self.oauth = oauth
        self.gateway_factory = gateway_factory
        self.sessions = sessions
        self.source_statuses = source_statuses

    def configure(
        self,
        profile_id: UUID,
        *,
        expected_permission_id: str | None = None,
        expected_email: str | None = None,
    ) -> SheetsProfile:
        with self.sessions() as session:
            session.get_one(Profile, profile_id)
        profile = SheetsProfile.create(
            str(profile_id),
            expected_permission_id=expected_permission_id,
            expected_email=expected_email,
        )
        if self.profiles.exists(str(profile_id)):
            existing = self.profiles.load(str(profile_id))
            if existing.spreadsheet_id is not None:
                profile = SheetsProfile(
                    profile.profile_id,
                    profile.expected_permission_id,
                    profile.expected_email,
                    existing.spreadsheet_id,
                    existing.spreadsheet_url,
                    existing.workbook_token,
                )
        self.profiles.save(profile)
        return profile

    def authorize(
        self, profile_id: UUID, *, force: bool = False, interactive: bool = False
    ) -> None:
        self.oauth.authorize(str(profile_id), force=force, interactive=interactive)

    def sync(self, profile_id: UUID) -> SheetsSyncReport:
        profile_key = str(profile_id)
        with self.state.sync_lock(profile_key):
            run_id = self._start_run(profile_id)
            try:
                report = self._sync_locked(profile_id)
            except (KeyboardInterrupt, SystemExit):
                raise
            except Exception as error:
                code = self._safe_code(error)
                self._finish_failed(run_id, code)
                self.state.write(
                    profile_key,
                    {"last_status": "failed", "safe_error_code": code},
                )
                if isinstance(error, (WorkbookOwnershipError, SheetsSyncFailure)):
                    raise
                raise SheetsSyncFailure(code) from error
            self._finish_success(run_id, report)
            self.state.write(
                profile_key,
                {
                    "last_status": "succeeded",
                    "last_success_at": utc_now().isoformat(),
                    "safe_error_code": None,
                },
            )
            return report

    def _sync_locked(self, profile_id: UUID) -> SheetsSyncReport:
        key = str(profile_id)
        profile = self.profiles.load(key)
        with self.sessions() as session:
            session.get_one(Profile, profile_id)
        self.oauth.authorize(key, interactive=False)
        credentials = self.oauth.load(key)
        if credentials is None:
            raise SheetsSyncFailure("oauth_required")
        gateway = self.gateway_factory(credentials)
        identity = gateway.account_identity()
        try:
            profile = self.profiles.load(key).with_account(identity)
        except ValueError as error:
            raise SheetsSyncFailure("account_mismatch") from error

        created = False
        if profile.spreadsheet_id is None:
            token = secrets.token_urlsafe(24)
            binding = WorkbookBinding(key, WORKBOOK_SCHEMA_VERSION, token)
            workbook = gateway.create_workbook(f"Health Agent — {key[:8]}", binding)
            profile = profile.with_workbook(
                workbook.spreadsheet_id, workbook.spreadsheet_url, token
            )
            self.profiles.save(profile)
            created = True
        assert profile.spreadsheet_id is not None
        assert profile.workbook_token is not None
        expected_binding = WorkbookBinding(
            key, WORKBOOK_SCHEMA_VERSION, profile.workbook_token
        )
        if gateway.read_binding(profile.spreadsheet_id) != expected_binding:
            raise WorkbookOwnershipError("configured workbook binding mismatch")

        decision_report = DecisionReport()
        with self.sessions() as session:
            before = build_projection(
                session,
                profile_id,
                expected_binding,
                self.source_statuses(session, profile_id),
            )
            if not created:
                remote_rows = gateway.read_review_rows(profile.spreadsheet_id)
                decisions = parse_decisions(
                    remote_rows, before.known_reviews, profile_id
                )
                decision_report = apply_decisions(
                    session, profile_id, profile.spreadsheet_id, decisions
                )

        with self.sessions() as session:
            projection = build_projection(
                session,
                profile_id,
                expected_binding,
                self.source_statuses(session, profile_id),
            )
        gateway.replace_managed_tabs(profile.spreadsheet_id, projection.workbook)
        lab_rows, review_rows, source_rows = (
            len(sheet.rows) for sheet in projection.workbook.sheets
        )
        return SheetsSyncReport(
            "succeeded",
            decision_report.applied,
            decision_report.replayed,
            lab_rows,
            review_rows,
            source_rows,
            profile.spreadsheet_id,
        )

    def status(self, profile_id: UUID) -> SheetsStatus:
        key = str(profile_id)
        if not self.profiles.exists(key):
            return SheetsStatus(False, "missing", False, None, None, None)
        profile = self.profiles.load(key)
        state = self.state.read(key)
        return SheetsStatus(
            True,
            self.oauth.local_status(key),
            profile.spreadsheet_id is not None,
            _optional_string(state.get("last_status")),
            _optional_string(state.get("last_success_at")),
            _optional_string(state.get("safe_error_code")),
        )

    def _start_run(self, profile_id: UUID) -> UUID:
        with self.sessions() as session:
            run = SheetsSyncRun(profile_id=profile_id, status="running")
            session.add(run)
            session.flush()
            return run.id

    def _finish_failed(self, run_id: UUID, code: str) -> None:
        with self.sessions() as session:
            run = session.get_one(SheetsSyncRun, run_id)
            run.status = "failed"
            run.completed_at = utc_now()
            run.safe_error_code = code

    def _finish_success(self, run_id: UUID, report: SheetsSyncReport) -> None:
        with self.sessions() as session:
            run = session.get_one(SheetsSyncRun, run_id)
            run.status = "succeeded"
            run.completed_at = utc_now()
            run.decisions_applied = report.decisions_applied
            run.decisions_replayed = report.decisions_replayed
            run.lab_rows = report.lab_rows
            run.review_rows = report.review_rows
            run.source_rows = report.source_rows
            run.safe_error_code = None

    @staticmethod
    def _safe_code(error: BaseException) -> str:
        if isinstance(error, SheetsSyncFailure):
            return error.safe_code
        if isinstance(error, WorkbookOwnershipError):
            return "workbook_mismatch"
        if error.__class__.__module__.startswith("google"):
            return safe_sheets_error_code(error)
        name = error.__class__.__name__
        if name in {"ReviewGridError", "ReviewConflict"}:
            return "review_grid_invalid"
        return "sheets_sync_failed"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
