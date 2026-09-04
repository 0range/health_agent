from __future__ import annotations

import socket
from pathlib import Path
from threading import Thread
from time import sleep

import httpx
import pytest

from health_agent.whoop.auth_service import (
    begin_whoop_authorization,
    complete_whoop_authorization,
    wait_for_loopback_callback,
)
from health_agent.whoop.oauth import WhoopOAuth
from health_agent.whoop.tokens import TokenStore


def oauth_with_token_transport() -> WhoopOAuth:
    return WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/whoop/callback",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "access_token": "access-secret",
                        "refresh_token": "refresh-secret",
                        "expires_in": 3600,
                        "scope": "offline read:profile",
                    },
                )
            )
        ),
    )


def test_complete_verifies_identity_before_saving_per_profile_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    oauth = oauth_with_token_transport()
    pending = begin_whoop_authorization(oauth)
    profile_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"user_id": 10129})
        )
    )

    account = complete_whoop_authorization(
        oauth,
        store,
        "vitalii",
        "main",
        pending,
        {"state": pending.state, "code": "private-code"},
        http_client=profile_http,
    )

    assert account.external_user_id == 10129
    assert store.load("vitalii", "main") is not None
    assert store.load("partner", "main") is None


def test_failed_profile_verification_does_not_save_token(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    oauth = oauth_with_token_transport()
    pending = begin_whoop_authorization(oauth)

    with pytest.raises(RuntimeError):
        complete_whoop_authorization(
            oauth,
            store,
            "vitalii",
            "main",
            pending,
            {"state": pending.state, "code": "private-code"},
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(401, json={"access": "access-secret"})
                )
            ),
        )

    assert store.load("vitalii", "main") is None


def test_loopback_callback_captures_only_expected_path() -> None:
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    result: list[dict[str, str]] = []
    failures: list[BaseException] = []

    def receive() -> None:
        try:
            result.append(
                wait_for_loopback_callback(
                    f"http://127.0.0.1:{port}/whoop/callback", timeout_seconds=2
                )
            )
        except (OSError, TimeoutError, ValueError) as error:
            failures.append(error)

    thread = Thread(target=receive)
    thread.start()
    sleep(0.05)
    response = httpx.get(
        f"http://127.0.0.1:{port}/whoop/callback?state=12345678&code=abc",
        timeout=2,
    )
    thread.join(timeout=2)

    assert response.status_code == 200
    assert failures == []
    assert result == [{"state": "12345678", "code": "abc"}]


def test_callback_rejects_non_loopback_redirect() -> None:
    with pytest.raises(ValueError, match="loopback"):
        wait_for_loopback_callback("http://example.com:8765/callback", timeout_seconds=0)
