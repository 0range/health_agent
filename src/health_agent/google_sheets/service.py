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
        reset_unknown_creation: bool = False,
    ) -> SheetsProfile:
        key = str(profile_id)
        with self.state.sync_lock(key):
            return self._configure_locked(
                profile_id,
                expected_permission_id=expected_permission_id,
                expected_email=expected_email,
                reset_unknown_creation=reset_unknown_creation,
            )

    def _configure_locked(
        self,
        profile_id: UUID,
        *,
        expected_permission_id: str | None,
        expected_email: str | None,
        reset_unknown_creation: bool,
    ) -> SheetsProfile:
        with self.sessions() as session:
            session.get_one(Profile, profile_id)
        if self.profiles.exists(str(profile_id)):
            existing = self.profiles.load(str(profile_id))
            if (
                expected_permission_id is not None
                and existing.expected_permission_id
                not in {None, expected_permission_id}
            ) or (
                expected_email is not None
                and existing.expected_email not in {None, expected_email.casefold()}
            ):
                raise ValueError("Sheets profile is already bound to another account")
            expected_permission_id = (
                expected_permission_id or existing.expected_permission_id
            )
            expected_email = expected_email or existing.expected_email
        else:
            existing = None
        if reset_unknown_creation:
            if existing is None:
                raise ValueError("Sheets profile is not configured")
            existing = existing.reset_creation_fence()
        profile = SheetsProfile.create(
            str(profile_id),
            expected_permission_id=expected_permission_id,
            expected_email=expected_email,
        )
        if existing is not None:
            profile = SheetsProfile(
                profile.profile_id,
                profile.expected_permission_id,
                profile.expected_email,
                existing.spreadsheet_id,
                existing.spreadsheet_url,
                existing.workbook_token,
                existing.projection_initialized,
                existing.creation_state,
            )
        self.profiles.save(profile)
        return profile

    def authorize(
        self, profile_id: UUID, *, force: bool = False, interactive: bool = False
    ) -> None:
        key = str(profile_id)
        with self.state.sync_lock(key):
            self.oauth.authorize(key, force=force, interactive=interactive)

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

        if profile.spreadsheet_id is None:
            if profile.creation_state != "not_started":
                raise SheetsSyncFailure("workbook_creation_unknown")
            token = secrets.token_urlsafe(24)
            profile = profile.with_creation_started(token)
            self.profiles.save(profile)
            binding = WorkbookBinding(key, WORKBOOK_SCHEMA_VERSION, token, False)
            try:
                workbook = gateway.create_workbook(
                    f"Health Agent — {key[:8]} — {token[:8]}", binding
                )
            except Exception as error:
                self.profiles.save(profile.with_unknown_creation())
                raise SheetsSyncFailure("workbook_creation_unknown") from error
            profile = profile.with_workbook(
                workbook.spreadsheet_id, workbook.spreadsheet_url, token
            )
            self.profiles.save(profile)
        assert profile.spreadsheet_id is not None
        assert profile.workbook_token is not None
        expected_binding = WorkbookBinding(
            key,
            WORKBOOK_SCHEMA_VERSION,
            profile.workbook_token,
            profile.projection_initialized,
        )
        try:
            actual_binding = gateway.read_binding(profile.spreadsheet_id)
        except (TypeError, ValueError) as error:
            raise WorkbookOwnershipError(
                "configured workbook binding is invalid"
            ) from error
        if (
            actual_binding.profile_id != expected_binding.profile_id
            or actual_binding.schema_version != expected_binding.schema_version
            or actual_binding.workbook_token != expected_binding.workbook_token
            or (
                profile.projection_initialized
                and not actual_binding.projection_initialized
            )
        ):
            raise WorkbookOwnershipError("configured workbook binding mismatch")
        if actual_binding.projection_initialized and not profile.projection_initialized:
            profile = profile.with_initialized_projection()
            self.profiles.save(profile)
        spreadsheet_id = profile.spreadsheet_id
        workbook_token = profile.workbook_token
        assert spreadsheet_id is not None
        assert workbook_token is not None

        decision_report = DecisionReport()
        remote_rows = None
        projection_binding = WorkbookBinding(
            key, WORKBOOK_SCHEMA_VERSION, workbook_token, True
        )
        with self.sessions() as session:
            before = build_projection(
                session,
                profile_id,
                projection_binding,
                self.source_statuses(session, profile_id),
            )
            if profile.projection_initialized:
                remote_rows = gateway.read_review_rows(spreadsheet_id)
                decisions = parse_decisions(
                    remote_rows, before.known_reviews, profile_id
                )
                decision_report = apply_decisions(
                    session, profile_id, spreadsheet_id, decisions
                )

        with self.sessions() as session:
            projection = build_projection(
                session,
                profile_id,
                projection_binding,
                self.source_statuses(session, profile_id),
            )
        if (
            remote_rows is not None
            and gateway.read_review_rows(spreadsheet_id) != remote_rows
        ):
            raise SheetsSyncFailure("review_grid_changed")
        gateway.replace_managed_tabs(spreadsheet_id, projection.workbook)
        if not profile.projection_initialized:
            profile = profile.with_initialized_projection()
            self.profiles.save(profile)
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
            spreadsheet_id,
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
        if name in {
            "InvalidReviewTransition",
            "NoResultFound",
            "ReviewGridError",
            "ReviewConflict",
            "UnsupportedNormalization",
        }:
            return "review_grid_invalid"
        return "sheets_sync_failed"


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) else None
