from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from health_agent.config import Settings
from health_agent.google_sheets.sources import collect_source_statuses
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.whoop.models import WhoopConnection

from .helpers import add_profile


def test_source_status_is_profile_scoped_and_uses_safe_account_label(
    tmp_path, session: Session
) -> None:
    now = datetime.now(UTC)
    other = add_profile(session)
    session.add_all(
        (
            WhoopConnection(
                profile_id=DEFAULT_PROFILE_ID,
                account_name="main",
                auth_status="connected",
                granted_scopes=[],
                last_success_at=now - timedelta(hours=1),
            ),
            WhoopConnection(
                profile_id=other,
                account_name="other-private",
                auth_status="connected",
                granted_scopes=[],
                last_success_at=now,
            ),
        )
    )
    session.flush()
    settings = Settings(
        google_drive_root=tmp_path / "drive",
        gmail_root=tmp_path / "gmail",
        google_sheets_root=tmp_path / "sheets",
    )
    rows = collect_source_statuses(settings, session, DEFAULT_PROFILE_ID, now=now)
    assert [(row.source, row.account, row.freshness) for row in rows] == [
        ("whoop", "main", "fresh")
    ]
    assert str(other) not in repr(rows)
    assert "other-private" not in repr(rows)


def test_stale_whoop_status_contains_no_source_payload(
    tmp_path, session: Session
) -> None:
    now = datetime.now(UTC)
    session.add(
        WhoopConnection(
            profile_id=DEFAULT_PROFILE_ID,
            account_name="main",
            auth_status="connected",
            granted_scopes=["private-scope"],
            last_success_at=now - timedelta(days=2),
            last_error_code="safe_code",
        )
    )
    session.flush()
    settings = Settings(
        google_drive_root=tmp_path / "drive",
        gmail_root=tmp_path / "gmail",
        google_sheets_root=tmp_path / "sheets",
    )
    rendered = repr(
        collect_source_statuses(settings, session, DEFAULT_PROFILE_ID, now=now)
    )
    assert "stale" in rendered
    assert "private-scope" not in rendered
