from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.oauth import DriveOAuth, OAuthScopeError
from health_agent.google_drive.stores import LocalTokenStore


def test_rejects_token_with_any_scope_beyond_drive_readonly(tmp_path: Path) -> None:
    credentials = Credentials(
        token="secret",
        scopes=[DRIVE_READONLY_SCOPE, "https://www.googleapis.com/auth/drive"],
    )
    with pytest.raises(OAuthScopeError):
        DriveOAuth._require_readonly(credentials)


def test_loads_only_profile_token_and_repairs_private_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    path = tokens.save(
        "alice", json.dumps({"token": "do-not-log", "scopes": [DRIVE_READONLY_SCOPE]})
    )
    path.chmod(0o644)
    expected = Credentials(token="secret", scopes=[DRIVE_READONLY_SCOPE])
    seen_paths: list[str] = []

    def fake_load(filename: str, scopes: list[str]) -> Credentials:
        seen_paths.append(filename)
        assert scopes == [DRIVE_READONLY_SCOPE]
        return expected

    monkeypatch.setattr(Credentials, "from_authorized_user_file", fake_load)
    loaded = DriveOAuth(tmp_path / "client.json", tokens).load("alice")

    assert loaded is expected
    assert seen_paths == [str(path)]
    assert path.stat().st_mode & 0o077 == 0
    assert not tokens.exists("bob")


def test_rejects_persisted_token_that_declares_broader_scope(tmp_path: Path) -> None:
    tokens = LocalTokenStore(tmp_path / "profiles")
    tokens.save(
        "alice",
        json.dumps(
            {
                "token": "do-not-log",
                "scopes": [
                    DRIVE_READONLY_SCOPE,
                    "https://www.googleapis.com/auth/drive",
                ],
            }
        ),
    )
    with pytest.raises(OAuthScopeError, match="persisted"):
        DriveOAuth(tmp_path / "client.json", tokens).load("alice")


def test_authorize_uses_desktop_flow_with_exact_scope_and_private_token(
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
    result = DriveOAuth(client, tokens).authorize("alice")

    assert result is credentials
    assert calls["filename"] == str(client)
    assert calls["scopes"] == [DRIVE_READONLY_SCOPE]
    assert calls["run"] == {
        "port": 0,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    assert tokens.path_for("alice").stat().st_mode & 0o077 == 0
