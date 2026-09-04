from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from health_agent.whoop.oauth import (
    AUTHORIZATION_URL,
    WHOOP_SCOPES,
    WhoopOAuth,
    WhoopOAuthError,
    WhoopOAuthScopesError,
)


def test_authorization_url_uses_official_endpoint_and_all_required_scopes() -> None:
    oauth = WhoopOAuth("client-id", "client-secret", "http://127.0.0.1:8765/callback")

    url = oauth.authorization_url("12345678")
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)

    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == AUTHORIZATION_URL
    assert query["state"] == ["12345678"]
    assert query["response_type"] == ["code"]
    assert tuple(query["scope"][0].split()) == WHOOP_SCOPES


def test_callback_rejects_state_mismatch_without_exposing_code() -> None:
    with pytest.raises(WhoopOAuthError) as caught:
        WhoopOAuth.validate_callback(
            {"state": "badstate", "code": "private-code"}, "goodstat"
        )

    assert "private-code" not in str(caught.value)


def test_exchange_and_refresh_use_official_forms_and_rotated_refresh_token() -> None:
    requests: list[httpx.Request] = []
    responses = iter(
        (
            {
                "access_token": "access-one",
                "refresh_token": "refresh-one",
                "expires_in": 3600,
                "scope": "offline read:sleep",
                "token_type": "bearer",
            },
            {
                "access_token": "access-two",
                "refresh_token": "refresh-two",
                "expires_in": 3600,
                "scope": " ".join(WHOOP_SCOPES),
                "token_type": "bearer",
            },
        )
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=next(responses))

    now = datetime(2026, 9, 4, tzinfo=UTC)
    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        clock=lambda: now,
    )

    first = oauth.exchange_code("authorization-code")
    second = oauth.refresh(first.refresh_token)

    first_form = parse_qs(requests[0].content.decode())
    second_form = parse_qs(requests[1].content.decode())
    assert first_form["grant_type"] == ["authorization_code"]
    assert first_form["redirect_uri"] == ["http://127.0.0.1:8765/callback"]
    assert second_form["grant_type"] == ["refresh_token"]
    assert second_form["refresh_token"] == ["refresh-one"]
    assert second.refresh_token == "refresh-two"


def test_token_error_does_not_expose_response_or_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "client-secret authorization-code"})

    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(WhoopOAuthError) as caught:
        oauth.exchange_code("authorization-code")

    rendered = str(caught.value)
    assert "client-secret" not in rendered
    assert "authorization-code" not in rendered


def test_refresh_rejects_a_grant_that_loses_required_scopes() -> None:
    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "access_token": "access",
                        "refresh_token": "refresh",
                        "expires_in": 3600,
                        "scope": "offline read:profile",
                    },
                )
            )
        ),
    )

    with pytest.raises(WhoopOAuthScopesError, match="required scopes"):
        oauth.refresh("old-refresh")
