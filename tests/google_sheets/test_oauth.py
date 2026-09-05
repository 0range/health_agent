from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_sheets.config import SHEETS_SCOPES, SheetsProfile
from health_agent.google_sheets.oauth import (
    SheetsAccountMismatch,
    SheetsOAuth,
    SheetsOAuthScopeError,
)
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsTokenStore,
)
from health_agent.google_sheets.types import SheetsAccountIdentity, SheetsGateway


class IdentityGateway:
    def __init__(self, identity: SheetsAccountIdentity) -> None:
        self.identity = identity

    def account_identity(self) -> SheetsAccountIdentity:
        return self.identity


def _credentials(scopes: list[str] | None = None) -> Credentials:
    return Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client",
        client_secret="secret",
        scopes=scopes or sorted(SHEETS_SCOPES),
    )


def test_authorize_verifies_expected_identity_before_publishing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = str(uuid4())
    profiles = LocalSheetsProfileStore(tmp_path / "profiles")
    tokens = LocalSheetsTokenStore(tmp_path / "tokens")
    profiles.save(
        SheetsProfile.create(
            profile_id,
            expected_permission_id="expected",
            expected_email="me@example.com",
        )
    )
    oauth = SheetsOAuth(
        Path("missing"),
        profiles,
        tokens,
        cast(
            Callable[[Credentials], SheetsGateway],
            lambda _: IdentityGateway(
                SheetsAccountIdentity("different", "other@example.com")
            ),
        ),
    )
    monkeypatch.setattr(oauth, "stage", lambda *args, **kwargs: _credentials())
    with pytest.raises(SheetsAccountMismatch):
        oauth.authorize(profile_id)
    assert not tokens.exists(profile_id)


def test_load_rejects_extra_scope(tmp_path: Path) -> None:
    profile_id = str(uuid4())
    profiles = LocalSheetsProfileStore(tmp_path / "profiles")
    tokens = LocalSheetsTokenStore(tmp_path / "tokens")
    profiles.save(SheetsProfile.create(profile_id))
    credentials = _credentials(
        [*SHEETS_SCOPES, "https://www.googleapis.com/auth/drive"]
    )
    tokens.publish_verified(
        profile_id,
        SheetsAccountIdentity("permission", "me@example.com"),
        credentials.to_json(),
    )
    oauth = SheetsOAuth(
        Path("missing"),
        profiles,
        tokens,
        cast(
            Callable[[Credentials], SheetsGateway],
            lambda _: IdentityGateway(
                SheetsAccountIdentity("permission", "me@example.com")
            ),
        ),
    )
    with pytest.raises(SheetsOAuthScopeError):
        oauth.load(profile_id)


def test_authorize_reloads_profile_after_interactive_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_id = str(uuid4())
    profiles = LocalSheetsProfileStore(tmp_path / "profiles")
    tokens = LocalSheetsTokenStore(tmp_path / "tokens")
    profiles.save(SheetsProfile.create(profile_id))
    oauth = SheetsOAuth(
        Path("missing"),
        profiles,
        tokens,
        cast(
            Callable[[Credentials], SheetsGateway],
            lambda _: IdentityGateway(
                SheetsAccountIdentity("permission", "me@example.com")
            ),
        ),
    )

    def stage(*args, **kwargs):  # type: ignore[no-untyped-def]
        del args, kwargs
        profiles.save(
            profiles.load(profile_id)
            .with_creation_started("token_12345678")
            .with_workbook(
                "spreadsheet_123",
                "https://docs.google.com/spreadsheets/d/spreadsheet_123/edit",
                "token_12345678",
            )
        )
        return _credentials()

    monkeypatch.setattr(oauth, "stage", stage)
    oauth.authorize(profile_id, interactive=True)
    assert profiles.load(profile_id).spreadsheet_id == "spreadsheet_123"


def test_client_secrets_refuse_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "client.json"
    secret.write_text("{}")
    alias = tmp_path / "alias"
    alias.symlink_to(outside, target_is_directory=True)
    oauth = SheetsOAuth(
        alias / "client.json",
        LocalSheetsProfileStore(tmp_path / "profiles"),
        LocalSheetsTokenStore(tmp_path / "tokens"),
        cast(Callable[[Credentials], SheetsGateway], lambda _: None),
    )
    with pytest.raises(RuntimeError, match="symlink"):
        oauth._validate_client_secrets()
