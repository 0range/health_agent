from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from typer.testing import CliRunner

from health_agent import cli
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.whoop.status import WhoopStatus
from health_agent.whoop.sync import WhoopSyncReport


@contextmanager
def fake_session_scope(engine: object) -> Any:
    yield object()


def test_whoop_help_exposes_auth_status_and_sync() -> None:
    result = CliRunner().invoke(cli.app, ["whoop", "--help"])

    assert result.exit_code == 0
    assert "auth" in result.stdout
    assert "status" in result.stdout
    assert "sync" in result.stdout


def test_status_prints_only_safe_freshness_and_counts(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "build_engine", lambda settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        cli,
        "get_whoop_status",
        lambda session, profile_id, account: WhoopStatus(
            True,
            "connected",
            datetime(2026, 9, 4, tzinfo=UTC),
            None,
            True,
            100,
            99,
            88,
            42,
        ),
    )

    result = CliRunner().invoke(cli.app, ["whoop", "status"])

    assert result.exit_code == 0
    assert "auth=connected" in result.stdout
    assert "last_success=2026-09-04T00:00:00+00:00" in result.stdout
    assert "weight_available=true" in result.stdout
    assert "sleeps=88" in result.stdout
    assert "email" not in result.stdout


def test_sync_passes_selected_profile_account_and_full_mode(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_whoop_oauth", lambda settings: object())
    monkeypatch.setattr(cli, "TokenStore", lambda root: object())
    monkeypatch.setattr(cli, "WhoopClient", lambda *args: object())
    monkeypatch.setattr(cli, "build_engine", lambda settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)

    def fake_sync(
        session: object,
        profile_id: object,
        account: str,
        client: object,
        *,
        full: bool,
    ) -> WhoopSyncReport:
        captured.update(profile_id=profile_id, account=account, full=full)
        return WhoopSyncReport("succeeded", "backfill", None, 6, 6, 0, 0)

    monkeypatch.setattr(cli, "sync_whoop", fake_sync)

    result = CliRunner().invoke(
        cli.app,
        [
            "whoop",
            "sync",
            "--profile-id",
            str(DEFAULT_PROFILE_ID),
            "--account",
            "second",
            "--full",
        ],
    )

    assert result.exit_code == 0
    assert captured == {
        "profile_id": DEFAULT_PROFILE_ID,
        "account": "second",
        "full": True,
    }
    assert "status=succeeded" in result.stdout


def test_sync_returns_nonzero_for_safe_failure(monkeypatch: Any) -> None:
    monkeypatch.setattr(cli, "_whoop_oauth", lambda settings: object())
    monkeypatch.setattr(cli, "TokenStore", lambda root: object())
    monkeypatch.setattr(cli, "WhoopClient", lambda *args: object())
    monkeypatch.setattr(cli, "build_engine", lambda settings: object())
    monkeypatch.setattr(cli, "session_scope", fake_session_scope)
    monkeypatch.setattr(
        cli,
        "sync_whoop",
        lambda *args, **kwargs: WhoopSyncReport(
            "failed", "incremental", None, 0, 0, 0, 0, "reauth_required"
        ),
    )

    result = CliRunner().invoke(cli.app, ["whoop", "sync"])

    assert result.exit_code == 1
    assert "error=reauth_required" in result.stdout
