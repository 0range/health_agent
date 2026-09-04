from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.auth.exceptions import RefreshError
from google.oauth2.credentials import Credentials

from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.oauth import DriveOAuth, OAuthRequired, OAuthScopeError
from health_agent.google_drive.stores import LocalTokenStore
from health_agent.google_drive.types import DriveAccountIdentity

ALICE = "11111111-1111-4111-8111-111111111111"
BOB = "22222222-2222-4222-8222-222222222222"


def credentials_payload(*, scopes: list[str] | None = None) -> dict[str, object]:
    return {
        "token": "do-not-log",
        "refresh_token": "refresh-do-not-log",
        "token_uri": "https://oauth2.googleapis.com/token",
        "client_id": "client-do-not-log",
        "client_secret": "secret-do-not-log",
        "scopes": scopes or [DRIVE_READONLY_SCOPE],
    }


def test_rejects_token_with_any_scope_beyond_drive_readonly() -> None:
    credentials = Credentials(
        token="secret",
        scopes=[DRIVE_READONLY_SCOPE, "https://www.googleapis.com/auth/drive"],
    )
    with pytest.raises(OAuthScopeError):
        DriveOAuth._require_readonly(credentials)


def test_loads_only_profile_token_and_repairs_private_mode(tmp_path: Path) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    path = tokens.publish_verified(
        ALICE,
        DriveAccountIdentity("permission-a", "alice@example.com"),
        json.dumps(credentials_payload()),
    )
    path.chmod(0o644)

    loaded = DriveOAuth(tmp_path / "client.json", tokens).load(ALICE)

    assert loaded is not None
    assert loaded.token == "do-not-log"
    assert path.stat().st_mode & 0o077 == 0
    assert not tokens.exists(BOB)


def test_rejects_persisted_token_that_declares_broader_scope(tmp_path: Path) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    tokens.publish_verified(
        ALICE,
        DriveAccountIdentity("permission-a", "alice@example.com"),
        json.dumps(
            credentials_payload(
                scopes=[DRIVE_READONLY_SCOPE, "https://www.googleapis.com/auth/drive"]
            )
        ),
    )
    with pytest.raises(OAuthScopeError, match="persisted"):
        DriveOAuth(tmp_path / "client.json", tokens).load(ALICE)


def test_stage_uses_127_loopback_exact_scope_and_does_not_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    credentials = Credentials(token="secret", scopes=[DRIVE_READONLY_SCOPE])
    calls: dict[str, object] = {}

    class FakeFlow:
        def run_local_server(self, **kwargs: object) -> Credentials:
            calls["run"] = kwargs
            return credentials

    def fake_flow(filename: str, scopes: list[str]) -> FakeFlow:
        calls["filename"] = filename
        calls["scopes"] = scopes
        return FakeFlow()

    monkeypatch.setattr(
        "health_agent.google_drive.oauth.InstalledAppFlow.from_client_secrets_file",
        fake_flow,
    )
    result = DriveOAuth(client, tokens).stage(ALICE, force=True, interactive=True)

    assert result is credentials
    assert calls["filename"] == str(client)
    assert calls["scopes"] == [DRIVE_READONLY_SCOPE]
    assert calls["run"] == {
        "host": "127.0.0.1",
        "port": 0,
        "timeout_seconds": 300,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    assert not tokens.exists(ALICE)


def test_mismatched_account_cannot_overwrite_existing_binding(tmp_path: Path) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    original = tokens.publish_verified(
        ALICE,
        DriveAccountIdentity("permission-a", "alice@example.com"),
        json.dumps(credentials_payload()),
    ).read_bytes()
    with pytest.raises(ValueError, match="another health profile"):
        tokens.publish_verified(
            BOB,
            DriveAccountIdentity("permission-a", "alice@example.com"),
            json.dumps(credentials_payload()),
        )
    assert tokens.path_for(ALICE).read_bytes() == original
    assert not tokens.exists(BOB)

    with pytest.raises(ValueError, match="already bound"):
        tokens.publish_verified(
            ALICE,
            DriveAccountIdentity("permission-b", "bob@example.com"),
            json.dumps(credentials_payload()),
        )
    assert tokens.path_for(ALICE).read_bytes() == original


def test_refresh_failure_becomes_explicit_reauthorization_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    oauth = DriveOAuth(tmp_path / "client.json", LocalTokenStore(tmp_path / "profiles"))

    class ExpiredCredentials:
        expired = True
        refresh_token = "refresh"
        valid = False

        def refresh(self, request: object) -> None:
            raise RefreshError("revoked")

    monkeypatch.setattr(oauth, "load", lambda profile_id: ExpiredCredentials())

    with pytest.raises(OAuthRequired, match="reauthorization"):
        oauth.stage(ALICE)
