from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.oauth2.credentials import Credentials

from health_agent.gmail.config import GMAIL_READONLY_SCOPE
from health_agent.gmail.oauth import GmailOAuth, OAuthScopeError
from health_agent.gmail.stores import LocalGmailTokenStore

PROFILE = "11111111-1111-1111-1111-111111111111"


def test_rejects_persisted_broader_scope(tmp_path: Path) -> None:
    tokens = LocalGmailTokenStore(tmp_path / "gmail")
    tokens.save(
        PROFILE,
        "personal",
        json.dumps(
            {
                "token": "secret",
                "scopes": [GMAIL_READONLY_SCOPE, "https://mail.google.com/"],
            }
        ),
    )
    with pytest.raises(OAuthScopeError):
        GmailOAuth(tmp_path / "client.json", tokens).load(PROFILE, "personal")


def test_authorize_uses_exact_scope_and_account_specific_private_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tokens = LocalGmailTokenStore(tmp_path / "gmail")
    client = tmp_path / "client.json"
    client.write_text("{}", encoding="utf-8")
    credentials = Credentials(token="secret", scopes=[GMAIL_READONLY_SCOPE])
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
        "health_agent.gmail.oauth.InstalledAppFlow.from_client_secrets_file",
        fake_flow,
    )
    result = GmailOAuth(client, tokens).authorize(PROFILE, "personal")

    assert result is credentials
    assert calls["scopes"] == [GMAIL_READONLY_SCOPE]
    assert calls["run"] == {
        "port": 0,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    token = tokens.path_for(PROFILE, "personal")
    assert token.stat().st_mode & 0o077 == 0
    assert not tokens.exists(PROFILE, "work")
