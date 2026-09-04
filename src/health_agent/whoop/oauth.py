from __future__ import annotations

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

import httpx

from health_agent.whoop.tokens import WhoopToken

AUTHORIZATION_URL = "https://api.prod.whoop.com/oauth/oauth2/auth"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
WHOOP_SCOPES = (
    "offline",
    "read:profile",
    "read:body_measurement",
    "read:cycles",
    "read:recovery",
    "read:sleep",
    "read:workout",
)


class WhoopOAuthError(RuntimeError):
    """Safe OAuth failure without a response body or credential value."""


class WhoopOAuthScopesError(WhoopOAuthError):
    """A refreshed grant no longer contains every required WHOOP scope."""


class WhoopOAuth:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if not client_id or not client_secret:
            raise WhoopOAuthError("WHOOP client credentials are not configured")
        self._client_id = client_id
        self._client_secret = client_secret
        self.redirect_uri = redirect_uri
        self._http = http_client or httpx.Client(timeout=30)
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def new_state() -> str:
        """WHOOP documents an eight-character OAuth state."""
        return secrets.token_hex(4)

    def authorization_url(self, state: str) -> str:
        if len(state) != 8:
            raise WhoopOAuthError("WHOOP OAuth state must be eight characters")
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self.redirect_uri,
                "response_type": "code",
                "scope": " ".join(WHOOP_SCOPES),
                "state": state,
            }
        )
        return f"{AUTHORIZATION_URL}?{query}"

    @staticmethod
    def validate_callback(query: dict[str, str], expected_state: str) -> str:
        if query.get("state") != expected_state:
            raise WhoopOAuthError("WHOOP OAuth callback state did not match")
        if "error" in query:
            raise WhoopOAuthError("WHOOP authorization was not completed")
        code = query.get("code")
        if not code:
            raise WhoopOAuthError("WHOOP OAuth callback did not include a code")
        return code

    def exchange_code(self, code: str) -> WhoopToken:
        return self._request_token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "redirect_uri": self.redirect_uri,
            }
        )

    def refresh(self, refresh_token: str) -> WhoopToken:
        token = self._request_token(
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "scope": "offline",
            }
        )
        if set(WHOOP_SCOPES).difference(token.scopes):
            raise WhoopOAuthScopesError("WHOOP refresh did not grant required scopes")
        return token

    def _request_token(self, data: dict[str, str]) -> WhoopToken:
        try:
            response = self._http.post(TOKEN_URL, data=data)
        except httpx.HTTPError as error:
            raise WhoopOAuthError(
                "WHOOP token endpoint is temporarily unavailable"
            ) from error
        if response.status_code != 200:
            raise WhoopOAuthError(
                f"WHOOP token endpoint returned status {response.status_code}"
            )
        try:
            payload = response.json()
            access_token = str(payload["access_token"])
            refresh_token = str(payload["refresh_token"])
            expires_in = int(payload["expires_in"])
            raw_scopes = payload.get("scope", "")
            if isinstance(raw_scopes, str):
                scopes = tuple(raw_scopes.split())
            else:
                scopes = tuple(str(item) for item in raw_scopes)
            token_type = str(payload.get("token_type", "bearer"))
        except (KeyError, TypeError, ValueError) as error:
            raise WhoopOAuthError(
                "WHOOP token endpoint returned an invalid response"
            ) from error
        if not access_token or not refresh_token or expires_in <= 0:
            raise WhoopOAuthError("WHOOP token endpoint returned an invalid response")
        return WhoopToken(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=self._clock() + timedelta(seconds=expires_in),
            scopes=scopes,
            token_type=token_type,
        )
