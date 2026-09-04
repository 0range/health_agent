"""Desktop OAuth with one private token file per local profile."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.google_drive.config import DRIVE_READONLY_SCOPE
from health_agent.google_drive.stores import LocalTokenStore


class OAuthScopeError(RuntimeError):
    """Raised when persisted credentials contain permissions outside read-only Drive."""


class DriveOAuth:
    def __init__(self, client_secrets_path: Path, tokens: LocalTokenStore) -> None:
        self.client_secrets_path = Path(client_secrets_path)
        self.tokens = tokens

    def authorize(self, profile_id: str) -> Credentials:
        credentials = self.load(profile_id)
        if credentials is None or (
            not credentials.valid
            and not (credentials.expired and credentials.refresh_token)
        ):
            self._validate_client_secrets()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets_path), [DRIVE_READONLY_SCOPE]
            )
            credentials = flow.run_local_server(
                port=0,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
            )
        elif credentials.expired and credentials.refresh_token:
            credentials.refresh(Request())
        if not credentials.valid:
            raise RuntimeError("Google OAuth credentials are not valid")
        self._require_readonly(credentials)
        self.tokens.save(profile_id, credentials.to_json())
        return credentials

    def load(self, profile_id: str) -> Credentials | None:
        path = self.tokens.path_for(profile_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("refusing non-regular OAuth token file")
        path.chmod(0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        stored_scopes = payload.get("scopes") if isinstance(payload, dict) else None
        if not isinstance(stored_scopes, list) or set(stored_scopes) != {
            DRIVE_READONLY_SCOPE
        }:
            raise OAuthScopeError(
                "persisted Google OAuth token must declare only Drive read-only access"
            )
        credentials = Credentials.from_authorized_user_file(
            str(path), [DRIVE_READONLY_SCOPE]
        )
        self._require_readonly(credentials)
        return credentials

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
