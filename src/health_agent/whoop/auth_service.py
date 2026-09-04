from __future__ import annotations

import ipaddress
import webbrowser
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.whoop.client import API_BASE_URL, PROFILE_PATH, WhoopApiError
from health_agent.whoop.models import WhoopConnection
from health_agent.whoop.oauth import WHOOP_SCOPES, WhoopOAuth, WhoopOAuthError
from health_agent.whoop.repository import (
    register_authorized_connection,
    validate_registration_target,
)
from health_agent.whoop.tokens import TokenStore, WhoopToken


@dataclass(frozen=True, slots=True)
class PendingWhoopAuthorization:
    state: str
    url: str


@dataclass(frozen=True, slots=True)
class AuthorizedWhoopAccount:
    external_user_id: int
    granted_scopes: tuple[str, ...]
    token: WhoopToken


def begin_whoop_authorization(oauth: WhoopOAuth) -> PendingWhoopAuthorization:
    """Return data usable by either the future management UI or the CLI."""
    state = oauth.new_state()
    return PendingWhoopAuthorization(state=state, url=oauth.authorization_url(state))


def complete_whoop_authorization(
    oauth: WhoopOAuth,
    pending: PendingWhoopAuthorization,
    callback_query: dict[str, str],
    *,
    http_client: httpx.Client | None = None,
) -> AuthorizedWhoopAccount:
    """Exchange and verify a candidate without publishing its token yet."""
    code = oauth.validate_callback(callback_query, pending.state)
    token = oauth.exchange_code(code)
    missing_scopes = set(WHOOP_SCOPES).difference(token.scopes)
    if missing_scopes:
        raise WhoopOAuthError("WHOOP did not grant every required read scope")
    client = http_client or httpx.Client(timeout=30)
    try:
        response = client.get(
            f"{API_BASE_URL}{PROFILE_PATH}",
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
    except httpx.TransportError as error:
        raise WhoopApiError(
            "WHOOP profile verification is temporarily unavailable"
        ) from error
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
        raise WhoopApiError(
            "WHOOP profile verification returned an invalid response"
        ) from error
    return AuthorizedWhoopAccount(external_user_id, token.scopes, token)


def validate_whoop_authorization_target(
    session: Session,
    token_store: TokenStore,
    profile_id: UUID,
    profile_key: str,
    account_name: str,
) -> None:
    """Fail before opening a browser if the local profile/account is invalid."""
    token_store.validate_target(profile_key, account_name)
    with token_store.operation(profile_key, account_name):
        token_store.recover(
            profile_key,
            account_name,
            _committed_token_generation(session, profile_id, account_name),
        )
        validate_registration_target(session, profile_id, account_name)


def publish_whoop_authorization(
    session_context: Callable[[], AbstractContextManager[Session]],
    token_store: TokenStore,
    profile_id: UUID,
    profile_key: str,
    account_name: str,
    authorized: AuthorizedWhoopAccount,
) -> None:
    """Atomically expose a verified token and its matching database connection."""
    if set(WHOOP_SCOPES).difference(authorized.granted_scopes):
        raise WhoopOAuthError("WHOOP did not grant every required read scope")
    with token_store.operation(profile_key, account_name):
        with session_context() as recovery_session:
            token_store.recover(
                profile_key,
                account_name,
                _committed_token_generation(recovery_session, profile_id, account_name),
            )
        with token_store.replacement(
            profile_key, account_name, authorized.token
        ) as replacement:
            with session_context() as session:
                validate_registration_target(session, profile_id, account_name)
                connection = register_authorized_connection(
                    session,
                    profile_id,
                    account_name,
                    authorized.external_user_id,
                    authorized.granted_scopes,
                )
                connection.token_generation = replacement.generation
                replacement.publish()
            replacement.commit()


def _committed_token_generation(
    session: Session, profile_id: UUID, account_name: str
) -> UUID | None:
    return session.scalar(
        select(WhoopConnection.token_generation).where(
            WhoopConnection.profile_id == profile_id,
            WhoopConnection.account_name == account_name,
        )
    )


def open_and_wait_for_whoop_authorization(
    oauth: WhoopOAuth,
    *,
    opener: Any = webbrowser.open,
    timeout_seconds: float = 300,
) -> tuple[PendingWhoopAuthorization, dict[str, str]]:
    pending = begin_whoop_authorization(oauth)
    opener(pending.url)
    query = wait_for_loopback_callback(
        oauth.redirect_uri, timeout_seconds=timeout_seconds
    )
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
                for key, values in parse_qs(
                    request.query, keep_blank_values=True
                ).items()
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
