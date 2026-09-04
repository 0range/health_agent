"""Desktop OAuth staged before an atomic, verified account binding is published."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.stores import LocalTokenStore
from health_agent.google_drive.types import DriveAccountIdentity


class OAuthScopeError(RuntimeError):
    """Persisted credentials contain permissions outside read-only Drive."""


class OAuthRequired(RuntimeError):
    """The user must explicitly authorize or reauthorize the connector."""


class DriveOAuth:
    def __init__(self, client_secrets_path: Path, tokens: LocalTokenStore) -> None:
        self.client_secrets_path = Path(client_secrets_path)
        self.tokens = tokens

    def stage(
        self,
        profile_id: str,
        *,
        force: bool = False,
        interactive: bool = False,
    ) -> Credentials:
        """Return usable credentials without changing the verified token file."""
        credentials = None if force else self.load(profile_id)
        if credentials is None:
            if not interactive:
                raise OAuthRequired("Google Drive authorization is missing")
            self._validate_client_secrets()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_path), [DRIVE_READONLY_SCOPE]
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
                raise OAuthRequired("Google Drive reauthorization is required") from error
        if not credentials.valid:
            raise OAuthRequired("Google Drive reauthorization is required")
        self._require_readonly(credentials)
        return credentials

    def publish_verified(
        self,
        profile_id: str,
        credentials: Credentials,
        identity: DriveAccountIdentity,
    ) -> Path:
        self._require_readonly(credentials)
        return self.tokens.publish_verified(profile_id, identity, credentials.to_json())

    def load(self, profile_id: str) -> Credentials | None:
        verified = self.tokens.load_verified(profile_id)
        if verified is None:
            return None
        _, payload = verified
        stored_scopes = payload.get("scopes")
        if not isinstance(stored_scopes, list) or set(stored_scopes) != {
            DRIVE_READONLY_SCOPE
        }:
            raise OAuthScopeError(
                "persisted Google OAuth token must declare only Drive read-only access"
            )
        credentials = Credentials.from_authorized_user_info(
            payload, [DRIVE_READONLY_SCOPE]
        )
        self._require_readonly(credentials)
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
        if not self.client_secrets_path.is_file() or self.client_secrets_path.is_symlink():
            raise FileNotFoundError(
                f"Google OAuth Desktop client file not found: {self.client_secrets_path}"
            )
        self.client_secrets_path.chmod(0o600)

    @staticmethod
    def _require_readonly(credentials: Credentials) -> None:
        actual = set(credentials.granted_scopes or credentials.scopes or ())
        if actual != {DRIVE_READONLY_SCOPE}:
            raise OAuthScopeError("Google OAuth token must grant only Drive read-only access")
