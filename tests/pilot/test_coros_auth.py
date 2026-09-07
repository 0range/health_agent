from __future__ import annotations

import json
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from health_agent.pilot.coros import CorosHTTPTransport
from health_agent.pilot.coros_auth import CorosAuthError, CorosOAuth


def _oauth_handler(requests: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/.well-known/oauth-protected-resource/mcp":
            return httpx.Response(
                200,
                json={
                    "resource": "https://mcpus.coros.com/mcp",
                    "authorization_servers": ["https://mcpus.coros.com"],
                },
            )
        if request.url.path == "/.well-known/oauth-authorization-server":
            return httpx.Response(
                200,
                json={
                    "issuer": "https://mcpus.coros.com",
                    "authorization_endpoint": "https://mcpus.coros.com/oauth2/authorize",
                    "token_endpoint": "https://mcpus.coros.com/oauth2/token",
                    "registration_endpoint": "https://mcpus.coros.com/connect/register",
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if request.url.path == "/connect/register":
            return httpx.Response(201, json={"client_id": "dynamic-client"})
        if request.url.path == "/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "openid offline_access mcp.tools",
                },
            )
        raise AssertionError(request.url)

    return handler


def test_pkce_dynamic_registration_callback_and_private_storage(tmp_path):
    requests: list[httpx.Request] = []
    client = httpx.Client(transport=httpx.MockTransport(_oauth_handler(requests)))
    oauth = CorosOAuth(
        tmp_path, http_client=client, clock=lambda: datetime(2026, 9, 7, tzinfo=UTC)
    )

    url = oauth.authorization_url("http://127.0.0.1:8766/callback")
    query = parse_qs(urlsplit(url).query)
    assert query["client_id"] == ["dynamic-client"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["resource"] == ["https://mcpus.coros.com/mcp"]
    oauth.exchange_callback(
        f"http://127.0.0.1:8766/callback?code=secret-code&state={query['state'][0]}"
    )

    assert oauth.status()
    assert (tmp_path / "token.json").stat().st_mode & 0o777 == 0o600
    token_request = requests[-1]
    token_form = parse_qs(token_request.content.decode())
    assert token_form["code_verifier"]
    assert token_form["code"] == ["secret-code"]
    assert "secret-code" not in repr(oauth)


def test_callback_rejects_state_without_leaking_code(tmp_path):
    oauth = CorosOAuth(
        tmp_path,
        http_client=httpx.Client(transport=httpx.MockTransport(_oauth_handler([]))),
    )
    oauth.authorization_url("http://127.0.0.1:8766/callback")
    with pytest.raises(CorosAuthError) as caught:
        oauth.exchange_callback(
            "http://127.0.0.1:8766/callback?code=private-code&state=wrong"
        )
    assert "private-code" not in str(caught.value)


def test_http_transport_initializes_and_calls_only_allowlisted_reads(tmp_path):
    oauth_requests: list[httpx.Request] = []
    oauth_client = httpx.Client(
        transport=httpx.MockTransport(_oauth_handler(oauth_requests))
    )
    oauth = CorosOAuth(tmp_path, http_client=oauth_client)
    url = oauth.authorization_url("http://127.0.0.1:8766/callback")
    state = parse_qs(urlsplit(url).query)["state"][0]
    oauth.exchange_callback(f"http://127.0.0.1:8766/callback?code=code&state={state}")

    mcp_requests: list[httpx.Request] = []

    def mcp_handler(request: httpx.Request) -> httpx.Response:
        mcp_requests.append(request)
        payload = json.loads(request.content)
        if payload["method"] == "initialize":
            return httpx.Response(
                200,
                headers={"Mcp-Session-Id": "session"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "serverInfo": {"name": "COROS", "version": "1"},
                    },
                },
            )
        return httpx.Response(
            200,
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"structuredContent": {"activities": [{"id": "a1"}]}},
            },
        )

    transport = CorosHTTPTransport(
        oauth, http_client=httpx.Client(transport=httpx.MockTransport(mcp_handler))
    )
    result = transport.call_tool(
        "querySportRecords", {"startDate": "2026-09-01", "endDate": "2026-09-07"}
    )
    assert result["structuredContent"]["activities"] == [{"id": "a1"}]
    assert mcp_requests[-1].headers["mcp-session-id"] == "session"
    assert mcp_requests[-1].headers["authorization"] == "Bearer access"
    with pytest.raises(ValueError, match="read tool"):
        transport.call_tool("generateTrainingPlan", {})
    assert len(mcp_requests) == 2


def test_sse_mcp_response_is_decoded(tmp_path):
    # Exercise parser independently of auth setup details.
    payload = (
        'event: message\ndata: {"jsonrpc":"2.0","id":2,"result":{"activities":[]}}\n\n'
    )
    assert CorosHTTPTransport._response_payload(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, text=payload)
    )["result"] == {"activities": []}
