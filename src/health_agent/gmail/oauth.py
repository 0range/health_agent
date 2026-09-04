"""Desktop OAuth for exact Gmail read-only access per profile/account."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.gmail.config import GMAIL_READONLY_SCOPE
from health_agent.gmail.stores import LocalGmailTokenStore


class OAuthScopeError(RuntimeError):
    """Persisted credentials contain scopes outside Gmail read-only."""


class GmailOAuth:
    def __init__(self, client_secrets: Path, tokens: LocalGmailTokenStore) -> None:
        self.client_secrets = Path(client_secrets)
        self.tokens = tokens

    def authorize(
        self, profile_id: str, account_id: str, *, force: bool = False
    ) -> Credentials:
        credentials = None if force else self.load(profile_id, account_id)
        if credentials is None or (
            not credentials.valid
            and not (credentials.expired and credentials.refresh_token)
        ):
            self._validate_client_secrets()
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets), [GMAIL_READONLY_SCOPE]
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
        self.tokens.save(profile_id, account_id, credentials.to_json())
        return credentials

    def load(self, profile_id: str, account_id: str) -> Credentials | None:
        path = self.tokens.path_for(profile_id, account_id)
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("refusing non-regular Gmail OAuth token file")
        path.chmod(0o600)
        value = json.loads(path.read_text(encoding="utf-8"))
        scopes = value.get("scopes") if isinstance(value, dict) else None
        if not isinstance(scopes, list) or set(scopes) != {GMAIL_READONLY_SCOPE}:
            raise OAuthScopeError(
                "persisted OAuth token must declare only Gmail read-only access"
            )
        credentials = Credentials.from_authorized_user_file(
            str(path), [GMAIL_READONLY_SCOPE]
        )
        self._require_readonly(credentials)
        return credentials

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
