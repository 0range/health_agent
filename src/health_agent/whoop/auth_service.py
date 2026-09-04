from __future__ import annotations

import ipaddress
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from health_agent.whoop.client import API_BASE_URL, PROFILE_PATH, WhoopApiError
from health_agent.whoop.oauth import WhoopOAuth
from health_agent.whoop.tokens import TokenStore


@dataclass(frozen=True, slots=True)
class PendingWhoopAuthorization:
    state: str
    url: str


@dataclass(frozen=True, slots=True)
class AuthorizedWhoopAccount:
    external_user_id: int
    granted_scopes: tuple[str, ...]


def begin_whoop_authorization(oauth: WhoopOAuth) -> PendingWhoopAuthorization:
    """Return data usable by either the future management UI or the CLI."""
    state = oauth.new_state()
    return PendingWhoopAuthorization(state=state, url=oauth.authorization_url(state))


def complete_whoop_authorization(
    oauth: WhoopOAuth,
    token_store: TokenStore,
    profile_key: str,
    account_name: str,
    pending: PendingWhoopAuthorization,
    callback_query: dict[str, str],
    *,
    http_client: httpx.Client | None = None,
) -> AuthorizedWhoopAccount:
    """Exchange, verify the WHOOP identity, then atomically retain the token."""
    code = oauth.validate_callback(callback_query, pending.state)
    token = oauth.exchange_code(code)
    client = http_client or httpx.Client(timeout=30)
    try:
        response = client.get(
            f"{API_BASE_URL}{PROFILE_PATH}",
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
    except httpx.TransportError as error:
        raise WhoopApiError("WHOOP profile verification is temporarily unavailable") from error
    if response.status_code != 200:
        raise WhoopApiError(
            f"WHOOP profile verification returned status {response.status_code}"
        )
    try:
        payload: Any = response.json()
        if not isinstance(payload, dict):
            raise TypeError
        external_user_id = int(payload["user_id"])
    except (KeyError, TypeError, ValueError) as error:
        raise WhoopApiError("WHOOP profile verification returned an invalid response") from error
    token_store.save(profile_key, account_name, token)
    return AuthorizedWhoopAccount(external_user_id, token.scopes)


def open_and_wait_for_whoop_authorization(
    oauth: WhoopOAuth,
    *,
    opener: Any = webbrowser.open,
    timeout_seconds: float = 300,
) -> tuple[PendingWhoopAuthorization, dict[str, str]]:
    pending = begin_whoop_authorization(oauth)
    opener(pending.url)
    query = wait_for_loopback_callback(oauth.redirect_uri, timeout_seconds=timeout_seconds)
    return pending, query


def wait_for_loopback_callback(
    redirect_uri: str, *, timeout_seconds: float = 300
) -> dict[str, str]:
    parsed = urlsplit(redirect_uri)
    if parsed.scheme != "http" or not parsed.hostname or parsed.port is None:
        raise ValueError("WHOOP redirect URI must be an HTTP loopback URL with a port")
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname == "localhost"
    if not is_loopback:
        raise ValueError("WHOOP redirect URI must use a loopback host")
    expected_path = parsed.path or "/"

    class CallbackHandler(BaseHTTPRequestHandler):
        callback_query: dict[str, str] | None = None

        def do_GET(self) -> None:
            request = urlsplit(self.path)
            if request.path != expected_path:
                self.send_error(404)
                return
            CallbackHandler.callback_query = {
                key: values[0]
                for key, values in parse_qs(request.query, keep_blank_values=True).items()
                if values
            }
            body = b"WHOOP connected. You can close this tab."
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = HTTPServer((parsed.hostname, parsed.port), CallbackHandler)
    try:
        server.timeout = timeout_seconds
        server.handle_request()
    finally:
        server.server_close()
    if CallbackHandler.callback_query is None:
        raise TimeoutError("Timed out waiting for WHOOP authorization")
    return CallbackHandler.callback_query
