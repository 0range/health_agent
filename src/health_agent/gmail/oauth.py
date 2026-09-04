"""Desktop OAuth for exact Gmail read-only access per profile/account."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.gmail.config import GMAIL_READONLY_SCOPE
from health_agent.gmail.stores import LocalGmailTokenStore


class OAuthScopeError(RuntimeError):
    """Persisted credentials contain scopes outside Gmail read-only."""


class OAuthRequired(RuntimeError):
    """The account needs an interactive authorization again."""


class GmailOAuth:
    def __init__(self, client_secrets: Path, tokens: LocalGmailTokenStore) -> None:
        self.client_secrets = Path(client_secrets)
        self.tokens = tokens

    def stage(
        self,
        profile_id: str,
        account_id: str,
        *,
        force: bool = False,
        interactive: bool = False,
    ) -> Credentials:
        credentials = None if force else self.load(profile_id, account_id)
        if credentials is None or (
            not credentials.valid
            and not (credentials.expired and credentials.refresh_token)
        ):
            if not interactive:
                raise OAuthRequired("Gmail authorization must be renewed")
            self._validate_client_secrets()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets), [GMAIL_READONLY_SCOPE]
            )
            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
                timeout_seconds=180,
            )
        elif credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except RefreshError as error:
                raise OAuthRequired("Gmail authorization must be renewed") from error
        if not credentials.valid:
            raise OAuthRequired("Gmail authorization must be renewed")
        self._require_readonly(credentials)
        return credentials

    def publish_verified(
        self,
        profile_id: str,
        account_id: str,
        credentials: Credentials,
        bound_email: str,
    ) -> Path:
        self._require_readonly(credentials)
        return self.tokens.publish_verified(
            profile_id, account_id, bound_email, credentials.to_json()
        )

    def load(self, profile_id: str, account_id: str) -> Credentials | None:
        verified = self.tokens.load_verified(profile_id, account_id)
        if verified is None:
            return None
        _, value = verified
        scopes = value.get("scopes")
        if not isinstance(scopes, list) or set(scopes) != {GMAIL_READONLY_SCOPE}:
            raise OAuthScopeError(
                "persisted OAuth token must declare only Gmail read-only access"
            )
        credentials = Credentials.from_authorized_user_info(
            value, [GMAIL_READONLY_SCOPE]
        )
        self._require_readonly(credentials)
        return credentials

    def local_status(self, profile_id: str, account_id: str) -> str:
        try:
            credentials = self.load(profile_id, account_id)
        except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            return "invalid"
        if credentials is None:
            return "missing"
        if credentials.valid:
            return "valid"
        if credentials.expired and credentials.refresh_token:
            return "refreshable"
        return "reauth_required"

    def _validate_client_secrets(self) -> None:
        if not self.client_secrets.is_file() or self.client_secrets.is_symlink():
            raise FileNotFoundError(
                f"Google OAuth Desktop client file not found: {self.client_secrets}"
            )
        self.client_secrets.chmod(0o600)

    @staticmethod
    def _require_readonly(credentials: Credentials) -> None:
        actual = set(credentials.granted_scopes or credentials.scopes or ())
        if actual != {GMAIL_READONLY_SCOPE}:
            raise OAuthScopeError("OAuth token must grant only Gmail read-only access")
