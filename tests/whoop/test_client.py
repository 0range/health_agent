from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Lock

import httpx
import pytest

from health_agent.whoop.client import (
    CYCLE_PATH,
    PROFILE_PATH,
    WhoopApiError,
    WhoopClient,
)
from health_agent.whoop.oauth import WhoopOAuth
from health_agent.whoop.tokens import TokenStore, WhoopToken


def make_client(
    tmp_path: Path,
    handler: httpx.MockTransport,
    *,
    sleeper: list[float] | None = None,
    expired: bool = False,
    max_attempts: int = 4,
) -> tuple[WhoopClient, TokenStore]:
    store = TokenStore(tmp_path / "tokens")
    store.save(
        "vitalii",
        "main",
        WhoopToken(
            access_token="old-access",
            refresh_token="old-refresh",
            expires_at=datetime.now(UTC) + timedelta(seconds=-1 if expired else 3600),
            scopes=("offline", "read:profile"),
        ),
    )
    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "access_token": "new-access",
                        "refresh_token": "new-refresh",
                        "expires_in": 3600,
                        "scope": "offline read:profile",
                    },
                )
            )
        ),
    )
    return (
        WhoopClient(
            oauth,
            store,
            "vitalii",
            "main",
            http_client=httpx.Client(transport=handler),
            sleeper=(sleeper.append if sleeper is not None else lambda seconds: None),
            max_attempts=max_attempts,
        ),
        store,
    )


def test_collection_follows_next_token_and_keeps_date_bounds(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(
                200, json={"records": [{"id": 1}], "next_token": "page-2"}
            )
        return httpx.Response(200, json={"records": [{"id": 2}], "next_token": None})

    client, _ = make_client(tmp_path, httpx.MockTransport(handler))
    start = datetime(2026, 8, 1, tzinfo=UTC)

    records = list(client.iter_collection(CYCLE_PATH, start=start))

    assert records == [{"id": 1}, {"id": 2}]
    assert requests[0].url.params["limit"] == "25"
    assert requests[0].url.params["start"] == "2026-08-01T00:00:00Z"
    assert requests[1].url.params["nextToken"] == "page-2"


def test_repeated_pagination_token_is_rejected(tmp_path: Path) -> None:
    client, _ = make_client(
        tmp_path,
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"records": [], "next_token": "same-token"}
            )
        ),
    )

    with pytest.raises(WhoopApiError, match="repeated"):
        list(client.iter_collection(CYCLE_PATH))


def test_429_uses_documented_reset_header_before_retry(tmp_path: Path) -> None:
    sleeps: list[float] = []
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"X-RateLimit-Reset": "3600"})
        return httpx.Response(200, json={"user_id": 1})

    client, _ = make_client(tmp_path, httpx.MockTransport(handler), sleeper=sleeps)

    assert client.get_object(PROFILE_PATH) == {"user_id": 1}
    assert sleeps == [3600.0]


def test_401_refreshes_rotated_token_and_retries_once(tmp_path: Path) -> None:
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        if len(authorizations) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"user_id": 1})

    client, store = make_client(
        tmp_path, httpx.MockTransport(handler), max_attempts=1
    )

    assert client.get_object(PROFILE_PATH) == {"user_id": 1}
    assert authorizations == ["Bearer old-access", "Bearer new-access"]
    assert store.load("vitalii", "main").refresh_token == "new-refresh"  # type: ignore[union-attr]


def test_expired_token_refreshes_before_api_request(tmp_path: Path) -> None:
    authorizations: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        authorizations.append(request.headers["Authorization"])
        return httpx.Response(200, json={"user_id": 1})

    client, _ = make_client(tmp_path, httpx.MockTransport(handler), expired=True)

    client.get_object(PROFILE_PATH)

    assert authorizations == ["Bearer new-access"]


def test_ordinary_client_error_is_not_retried_or_leaked(tmp_path: Path) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"token": "old-access"})

    client, _ = make_client(tmp_path, httpx.MockTransport(handler))

    with pytest.raises(WhoopApiError) as caught:
        client.get_object(PROFILE_PATH)

    assert calls == 1
    assert "old-access" not in str(caught.value)


def test_concurrent_clients_rotate_one_refresh_token_only_once(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "tokens")
    store.save(
        "vitalii",
        "main",
        WhoopToken(
            access_token="expired-access",
            refresh_token="single-use-refresh",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
            scopes=("offline", "read:profile"),
        ),
    )
    refresh_calls = 0
    counter_lock = Lock()

    def oauth_handler(request: httpx.Request) -> httpx.Response:
        nonlocal refresh_calls
        with counter_lock:
            refresh_calls += 1
        return httpx.Response(
            200,
            json={
                "access_token": "rotated-access",
                "refresh_token": "rotated-refresh",
                "expires_in": 3600,
                "scope": "offline read:profile",
            },
        )

    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(transport=httpx.MockTransport(oauth_handler)),
    )
    api_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json={"user_id": 1})
    )
    clients = [
        WhoopClient(
            oauth,
            store,
            "vitalii",
            "main",
            http_client=httpx.Client(transport=api_transport),
        )
        for _ in range(2)
    ]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda client: client.get_object(PROFILE_PATH), clients))

    assert results == [{"user_id": 1}, {"user_id": 1}]
    assert refresh_calls == 1
    assert store.load("vitalii", "main").refresh_token == "rotated-refresh"  # type: ignore[union-attr]
