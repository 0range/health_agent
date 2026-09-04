from __future__ import annotations

from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials
from sqlalchemy import Engine
from typer.testing import CliRunner

from health_agent.cli import app
from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.stores import LocalSyncStateStore, LocalTokenStore
from health_agent.google_drive.types import DriveAccountIdentity
from health_agent.models import DEFAULT_PROFILE_ID


def test_configure_and_status_are_profile_specific(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(tmp_path / "drive"))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    root = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"

    profile_id = str(DEFAULT_PROFILE_ID)
    configured = runner.invoke(app, ["drive", "configure", profile_id, root])
    status = runner.invoke(app, ["drive", "status", profile_id])

    assert configured.exit_code == 0
    assert f"status=configured profile={profile_id} roots=1" in configured.stdout
    assert status.exit_code == 0
    assert f"profile={profile_id}" in status.stdout
    assert "token=missing" in status.stdout
    assert "account_bound=no" in status.stdout
    assert "cursor=none" in status.stdout


def test_changing_roots_invalidates_old_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    runner = CliRunner()
    profile_id = str(DEFAULT_PROFILE_ID)
    first = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    second = "2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    assert runner.invoke(app, ["drive", "configure", profile_id, first]).exit_code == 0
    state = LocalSyncStateStore(drive_root)
    state.set_cursor(profile_id, "old-root-cursor")

    result = runner.invoke(app, ["drive", "configure", profile_id, second])

    assert result.exit_code == 0
    assert state.get_cursor(profile_id) is None


def test_configure_unknown_database_profile_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(tmp_path / "drive"))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    unknown = "22222222-2222-4222-8222-222222222222"
    result = CliRunner().invoke(
        app,
        ["drive", "configure", unknown, "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"],
    )

    assert result.exit_code != 0
    assert "profile does not exist" in result.output


def test_status_reports_invalid_token_without_remote_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    token_path = LocalTokenStore(drive_root).path_for(profile_id)
    token_path.write_text("not-json", encoding="utf-8")

    result = runner.invoke(app, ["drive", "status", profile_id])

    assert result.exit_code == 0
    assert "token=invalid" in result.stdout
    assert "account_bound=no" in result.stdout


def test_status_exposes_interrupted_sync_without_remote_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_database: Engine
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    LocalSyncStateStore(drive_root).begin_sync(profile_id, "full")

    result = runner.invoke(app, ["drive", "status", profile_id])

    assert result.exit_code == 0
    assert "sync_state=interrupted" in result.stdout
    assert "last_attempt=" in result.stdout


@pytest.mark.parametrize("failure", ["lookup", "mismatch"])
def test_failed_reauthorization_preserves_previous_verified_token(
    failure: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_database: Engine,
) -> None:
    drive_root = tmp_path / "drive"
    monkeypatch.setenv("GOOGLE_DRIVE_ROOT", str(drive_root))
    monkeypatch.setenv("DATABASE_URL", clean_database.url.render_as_string(hide_password=False))
    profile_id = str(DEFAULT_PROFILE_ID)
    runner = CliRunner()
    folder = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    assert runner.invoke(app, ["drive", "configure", profile_id, folder]).exit_code == 0
    tokens = LocalTokenStore(drive_root)
    old_credentials = Credentials(token="old", scopes=[DRIVE_READONLY_SCOPE])
    token_path = tokens.publish_verified(
        profile_id,
        DriveAccountIdentity("permission-old", "old@example.com"),
        old_credentials.to_json(),
    )
    original = token_path.read_bytes()
    staged = Credentials(token="new", scopes=[DRIVE_READONLY_SCOPE])
    monkeypatch.setattr("health_agent.cli.DriveOAuth.stage", lambda *args, **kwargs: staged)

    if failure == "lookup":
        def fail_lookup(credentials: Credentials) -> object:
            raise RuntimeError("identity lookup failed")

        monkeypatch.setattr(
            "health_agent.cli.GoogleDriveGateway.from_credentials", fail_lookup
        )
    else:
        class Gateway:
            def account_identity(self) -> DriveAccountIdentity:
                return DriveAccountIdentity("permission-new", "new@example.com")

        monkeypatch.setattr(
            "health_agent.cli.GoogleDriveGateway.from_credentials",
            lambda credentials: Gateway(),
        )

    result = runner.invoke(app, ["drive", "auth", profile_id])

    assert result.exit_code != 0
    assert token_path.read_bytes() == original
