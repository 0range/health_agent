"""Safe connector freshness projection for one profile."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.gmail.oauth import GmailOAuth
from health_agent.gmail.stores import (
    LocalGmailProfileStore,
    LocalGmailStateStore,
    LocalGmailTokenStore,
)
from health_agent.google_drive.oauth import DriveOAuth
from health_agent.google_drive.stores import (
    LocalSyncStateStore,
    LocalTokenStore,
)
from health_agent.google_sheets.oauth import SheetsOAuth
from health_agent.google_sheets.projection import SourceStatusRow
from health_agent.whoop.models import WhoopConnection


def _safe_regular_file(path: Path) -> bool:
    if path.is_symlink():
        raise RuntimeError("unsafe connector configuration")
    return path.is_file()


def _freshness(value: str | datetime | None, source: str, now: datetime) -> str:
    if value is None:
        return "never"
    try:
        timestamp = datetime.fromisoformat(value) if isinstance(value, str) else value
    except ValueError:
        return "invalid"
    if timestamp.tzinfo is None:
        return "invalid"
    threshold = timedelta(hours=36 if source == "gmail" else 8)
    return "fresh" if now - timestamp.astimezone(UTC) <= threshold else "stale"


def collect_source_statuses(
    settings: Settings,
    session: Session,
    profile_id: UUID,
    *,
    sheets_oauth: SheetsOAuth | None = None,
    now: datetime | None = None,
) -> tuple[SourceStatusRow, ...]:
    now = now or datetime.now(UTC)
    profile = str(profile_id)
    rows: list[SourceStatusRow] = []

    drive_profile_path = settings.google_drive_root / profile / "profile.json"
    if _safe_regular_file(drive_profile_path):
        drive_state = LocalSyncStateStore(settings.google_drive_root).run_state(profile)
        drive_auth = DriveOAuth(
            settings.google_drive_client_secrets,
            LocalTokenStore(settings.google_drive_root),
            settings.google_drive_http_timeout_seconds,
        ).local_status(profile)
        last_success = drive_state["last_success_at"]
        rows.append(
            SourceStatusRow(
                "drive",
                "main",
                drive_auth,
                drive_state["last_attempt_at"],
                last_success,
                safe_error=drive_state["last_error_code"],
                freshness=_freshness(last_success, "drive", now),
            )
        )

    gmail_profile_path = settings.gmail_root / profile / "profile.json"
    if _safe_regular_file(gmail_profile_path):
        gmail_profiles = LocalGmailProfileStore(settings.gmail_root)
        gmail_tokens = LocalGmailTokenStore(settings.gmail_root)
        gmail_states = LocalGmailStateStore(settings.gmail_root)
        gmail_oauth = GmailOAuth(settings.google_oauth_client_secrets, gmail_tokens)
        for account in gmail_profiles.load(profile).accounts:
            state = gmail_states.get_run_state(profile, account.account_id)
            rows.append(
                SourceStatusRow(
                    "gmail",
                    account.account_id,
                    gmail_oauth.local_status(profile, account.account_id),
                    state.last_attempt_at,
                    state.last_success_at,
                    safe_error=state.last_error_code,
                    freshness=_freshness(state.last_success_at, "gmail", now),
                )
            )

    whoop_rows = session.scalars(
        select(WhoopConnection)
        .where(WhoopConnection.profile_id == profile_id)
        .order_by(WhoopConnection.account_name)
    )
    for connection in whoop_rows:
        rows.append(
            SourceStatusRow(
                "whoop",
                connection.account_name,
                connection.auth_status,
                connection.last_attempt_at.isoformat()
                if connection.last_attempt_at
                else None,
                connection.last_success_at.isoformat()
                if connection.last_success_at
                else None,
                connection.retry_at.isoformat() if connection.retry_at else None,
                connection.last_error_code,
                _freshness(connection.last_success_at, "whoop", now),
            )
        )

    sheets_profile_path = settings.google_sheets_root / profile / "profile.json"
    if _safe_regular_file(sheets_profile_path):
        authorization = (
            sheets_oauth.local_status(profile) if sheets_oauth else "unknown"
        )
        rows.append(SourceStatusRow("sheets", "main", authorization))
    return tuple(sorted(rows, key=lambda row: (row.source, row.account)))
