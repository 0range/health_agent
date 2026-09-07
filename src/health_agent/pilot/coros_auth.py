"""OAuth 2.0 PKCE for the self-service COROS MCP endpoint."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import stat
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx

from health_agent.automation.storage import atomic_private_write

DISCOVERY_URL = "https://mcp.coros.com/.well-known/oauth-protected-resource/mcp"
SCOPES = "openid offline_access mcp.tools"


class CorosAuthError(RuntimeError):
    """OAuth failure whose message never contains credentials or callback codes."""


class CorosOAuth:
    """Discover COROS, dynamically register a public client, and retain its grant."""

    def __init__(
        self,
        root: Path,
        *,
        http_client: httpx.Client | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root
        self._http = http_client or httpx.Client(timeout=30, follow_redirects=True)
        self._clock = clock or (lambda: datetime.now(UTC))

    def authorization_url(self, redirect_uri: str) -> str:
        resource, metadata = self._discover()
        registration = self._register(metadata, redirect_uri)
        verifier = _verifier()
        state = secrets.token_urlsafe(24)
        pending = {
            "client_id": registration["client_id"],
            "redirect_uri": redirect_uri,
            "verifier": verifier,
            "state": state,
            "resource": resource,
            "token_endpoint": metadata["token_endpoint"],
        }
        self._write(self.root / "pending.json", pending)
        query = urlencode(
            {
                "response_type": "code",
                "client_id": registration["client_id"],
                "redirect_uri": redirect_uri,
                "scope": SCOPES,
                "code_challenge": _challenge(verifier),
                "code_challenge_method": "S256",
                "resource": resource,
                "state": state,
            }
        )
        return f"{metadata['authorization_endpoint']}?{query}"

    def exchange_callback(self, url: str) -> None:
        pending = self._read(self.root / "pending.json")
        if pending is None:
            raise CorosAuthError("COROS authorization has not been started")
        query = parse_qs(urlsplit(url).query)
        if query.get("state", [None])[0] != pending.get("state"):
            raise CorosAuthError("COROS OAuth callback state did not match")
        if "error" in query or not query.get("code", [None])[0]:
            raise CorosAuthError("COROS authorization was not completed")
        token = self._token_request(
            str(pending["token_endpoint"]),
            {
                "grant_type": "authorization_code",
                "client_id": str(pending["client_id"]),
                "code": str(query["code"][0]),
                "redirect_uri": str(pending["redirect_uri"]),
                "code_verifier": str(pending["verifier"]),
            },
            client_id=str(pending["client_id"]),
            resource=str(pending["resource"]),
            token_endpoint=str(pending["token_endpoint"]),
        )
        self._write(self.root / "token.json", token)
        (self.root / "pending.json").unlink(missing_ok=True)

    def status(self) -> bool:
        try:
            token = self._read(self.root / "token.json")
            return (
                token is not None
                and bool(token.get("access_token"))
                and bool(token.get("refresh_token"))
            )
        except (CorosAuthError, OSError, TypeError, ValueError):
            return False

    def access(self) -> tuple[str, str, str]:
        token = self._read(self.root / "token.json")
        if token is None:
            raise CorosAuthError("COROS is not authorized")
        expires_at = datetime.fromisoformat(str(token["expires_at"]))
        if expires_at <= self._clock() + timedelta(seconds=60):
            token = self._token_request(
                str(token["token_endpoint"]),
                {
                    "grant_type": "refresh_token",
                    "client_id": str(token["client_id"]),
                    "refresh_token": str(token["refresh_token"]),
                },
                client_id=str(token["client_id"]),
                resource=str(token["resource"]),
                token_endpoint=str(token["token_endpoint"]),
                prior_refresh=str(token["refresh_token"]),
            )
            self._write(self.root / "token.json", token)
        return (
            str(token.get("token_type", "Bearer")),
            str(token["access_token"]),
            str(token["resource"]),
        )

    def _discover(self) -> tuple[str, dict[str, Any]]:
        protected = self._get_json(DISCOVERY_URL, "protected-resource discovery")
        try:
            resource = str(protected["resource"])
            issuer = str(protected["authorization_servers"][0]).rstrip("/")
        except (KeyError, IndexError, TypeError) as error:
            raise CorosAuthError("COROS discovery response is invalid") from error
        metadata = self._get_json(
            f"{issuer}/.well-known/oauth-authorization-server",
            "authorization-server discovery",
        )
        required = ("authorization_endpoint", "token_endpoint", "registration_endpoint")
        if any(not isinstance(metadata.get(key), str) for key in required):
            raise CorosAuthError("COROS authorization metadata is invalid")
        if "S256" not in metadata.get("code_challenge_methods_supported", []):
            raise CorosAuthError(
                "COROS authorization server does not advertise PKCE S256"
            )
        return resource, metadata

    def _register(self, metadata: dict[str, Any], redirect_uri: str) -> dict[str, str]:
        try:
            response = self._http.post(
                str(metadata["registration_endpoint"]),
                json={
                    "client_name": "Health Agent COROS Reader",
                    "redirect_uris": [redirect_uri],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "scope": SCOPES,
                    "token_endpoint_auth_method": "none",
                },
            )
        except httpx.HTTPError as error:
            raise CorosAuthError("COROS client registration is unavailable") from error
        if response.status_code not in (200, 201):
            raise CorosAuthError(
                f"COROS client registration returned status {response.status_code}"
            )
        try:
            client_id = str(response.json()["client_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise CorosAuthError(
                "COROS client registration response is invalid"
            ) from error
        if not client_id:
            raise CorosAuthError("COROS client registration response is invalid")
        return {"client_id": client_id}

    def _token_request(
        self,
        endpoint: str,
        form: dict[str, str],
        *,
        client_id: str,
        resource: str,
        token_endpoint: str,
        prior_refresh: str | None = None,
    ) -> dict[str, Any]:
        try:
            response = self._http.post(endpoint, data=form)
        except httpx.HTTPError as error:
            raise CorosAuthError("COROS token endpoint is unavailable") from error
        if response.status_code != 200:
            raise CorosAuthError(
                f"COROS token endpoint returned status {response.status_code}"
            )
        try:
            body = response.json()
            access_token = str(body["access_token"])
            refresh_token = str(body.get("refresh_token") or prior_refresh or "")
            expires_in = int(body.get("expires_in", 3600))
        except (KeyError, TypeError, ValueError) as error:
            raise CorosAuthError("COROS token response is invalid") from error
        if not access_token or not refresh_token or expires_in <= 0:
            raise CorosAuthError("COROS token response is invalid")
        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": (self._clock() + timedelta(seconds=expires_in)).isoformat(),
            "token_type": str(body.get("token_type", "Bearer")),
            "scope": str(body.get("scope", SCOPES)),
            "client_id": client_id,
            "resource": resource,
            "token_endpoint": token_endpoint,
        }

    def _get_json(self, url: str, label: str) -> dict[str, Any]:
        try:
            response = self._http.get(url)
        except httpx.HTTPError as error:
            raise CorosAuthError(f"COROS {label} is unavailable") from error
        if response.status_code != 200:
            raise CorosAuthError(
                f"COROS {label} returned status {response.status_code}"
            )
        try:
            value = response.json()
        except ValueError as error:
            raise CorosAuthError(f"COROS {label} response is invalid") from error
        if not isinstance(value, dict):
            raise CorosAuthError(f"COROS {label} response is invalid")
        return value

    @staticmethod
    def _write(path: Path, value: dict[str, Any]) -> None:
        atomic_private_write(path, json.dumps(value, sort_keys=True).encode())

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        if not path.exists():
            return None
        info = path.lstat()
        if (
            path.is_symlink()
            or not stat.S_ISREG(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
        ):
            raise CorosAuthError("COROS credential file is not private")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CorosAuthError("COROS credential file is invalid") from error
        if not isinstance(value, dict):
            raise CorosAuthError("COROS credential file is invalid")
        return value


def _verifier() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def _challenge(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
