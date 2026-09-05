"""Dedicated exact-scope Google Sheets OAuth with verified account binding."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.google_sheets.config import SHEETS_SCOPES
from health_agent.google_sheets.stores import (
    LocalSheetsProfileStore,
    LocalSheetsTokenStore,
)
from health_agent.google_sheets.types import SheetsGateway


class SheetsOAuthScopeError(RuntimeError):
    """Credentials do not carry the connector's exact bounded scopes."""


class SheetsOAuthRequired(RuntimeError):
    """Interactive Google authorization is required."""


class SheetsAccountMismatch(RuntimeError):
    """The authorized Google account differs from the bound account."""


class SheetsOAuth:
    def __init__(
        self,
        client_secrets: Path,
        profiles: LocalSheetsProfileStore,
        tokens: LocalSheetsTokenStore,
        gateway_factory: Callable[[Credentials], SheetsGateway],
        *,
        timeout_seconds: int = 30,
    ) -> None:
        self.client_secrets = Path(client_secrets)
        self.profiles = profiles
        self.tokens = tokens
        self.gateway_factory = gateway_factory
        self.timeout_seconds = timeout_seconds

    def stage(
        self,
        profile_id: str,
        *,
        force: bool = False,
        interactive: bool = False,
    ) -> Credentials:
        credentials = None if force else self.load(profile_id)
        if credentials is None:
            if not interactive:
                raise SheetsOAuthRequired("Google Sheets authorization is missing")
            self._validate_client_secrets()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets), sorted(SHEETS_SCOPES)
            )
            credentials = flow.run_local_server(
                host="127.0.0.1",
                port=0,
                timeout_seconds=300,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
            )
        elif credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                raise SheetsOAuthRequired(
                    "Google Sheets reauthorization is required"
                ) from error
        if not credentials.valid:
            raise SheetsOAuthRequired("Google Sheets reauthorization is required")
        self._require_exact_scopes(credentials)
        return credentials

    def authorize(
        self,
        profile_id: str,
        *,
        force: bool = False,
        interactive: bool = False,
    ) -> None:
        profile = self.profiles.load(profile_id)
        credentials = self.stage(profile_id, force=force, interactive=interactive)
        identity = self.gateway_factory(credentials).account_identity()
        try:
            bound = profile.with_account(identity)
        except ValueError as error:
            raise SheetsAccountMismatch("authorized Google account mismatch") from error
        self.tokens.publish_verified(profile_id, identity, credentials.to_json())
        self.profiles.save(bound)

    def load(self, profile_id: str) -> Credentials | None:
        verified = self.tokens.load_verified(profile_id)
        if verified is None:
            return None
        _, payload = verified
        scopes = payload.get("scopes")
        if not isinstance(scopes, list) or set(scopes) != set(SHEETS_SCOPES):
            raise SheetsOAuthScopeError("persisted token has invalid scopes")
        credentials = Credentials.from_authorized_user_info(
            payload, sorted(SHEETS_SCOPES)
        )
        self._require_exact_scopes(credentials)
        return credentials

    def local_status(self, profile_id: str) -> str:
        try:
            credentials = self.load(profile_id)
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return "invalid"
        if credentials is None:
            return "missing"
        if credentials.valid:
            return "ready"
        if credentials.expired and credentials.refresh_token:
            return "refresh_required"
        return "reauth_required"

    def _validate_client_secrets(self) -> None:
        if not self.client_secrets.is_file() or self.client_secrets.is_symlink():
            raise FileNotFoundError("Google OAuth Desktop client file not found")
        self.client_secrets.chmod(0o600)

    @staticmethod
    def _require_exact_scopes(credentials: Credentials) -> None:
        actual = set(credentials.granted_scopes or credentials.scopes or ())
        if actual != set(SHEETS_SCOPES):
            raise SheetsOAuthScopeError("Google token must grant exact Sheets scopes")
