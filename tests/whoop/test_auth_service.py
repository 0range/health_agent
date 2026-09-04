from __future__ import annotations

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Thread
from time import sleep
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from health_agent.whoop.auth_service import (
    AuthorizedWhoopAccount,
    begin_whoop_authorization,
    complete_whoop_authorization,
    publish_whoop_authorization,
    validate_whoop_authorization_target,
    wait_for_loopback_callback,
)
from health_agent.whoop.oauth import WHOOP_SCOPES, WhoopOAuth, WhoopOAuthError
from health_agent.whoop.repository import WhoopRepositoryError
from health_agent.whoop.tokens import TokenStore, WhoopToken


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
                        "scope": " ".join(WHOOP_SCOPES),
                    },
                )
            )
        ),
    )


def test_complete_verifies_identity_and_returns_unpublished_candidate() -> None:
    oauth = oauth_with_token_transport()
    pending = begin_whoop_authorization(oauth)
    profile_http = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"user_id": 10129})
        )
    )

    account = complete_whoop_authorization(
        oauth,
        pending,
        {"state": pending.state, "code": "private-code"},
        http_client=profile_http,
    )

    assert account.external_user_id == 10129
    assert account.token.access_token == "access-secret"


def test_failed_profile_verification_does_not_save_token(tmp_path: Path) -> None:
    oauth = oauth_with_token_transport()
    pending = begin_whoop_authorization(oauth)

    with pytest.raises(RuntimeError):
        complete_whoop_authorization(
            oauth,
            pending,
            {"state": pending.state, "code": "private-code"},
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(
                        401, json={"access": "access-secret"}
                    )
                )
            ),
        )


def test_missing_required_scope_is_rejected_before_profile_request() -> None:
    oauth = WhoopOAuth(
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
    pending = begin_whoop_authorization(oauth)

    with pytest.raises(WhoopOAuthError, match="required read scope"):
        complete_whoop_authorization(
            oauth,
            pending,
            {"state": pending.state, "code": "private-code"},
            http_client=httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: pytest.fail("profile request was not expected")
                )
            ),
        )


def test_publish_restores_good_token_when_database_commit_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = WhoopToken(
        "old-access",
        "old-refresh",
        datetime.now(UTC) + timedelta(hours=1),
        WHOOP_SCOPES,
    )
    candidate = WhoopToken(
        "new-access",
        "new-refresh",
        datetime.now(UTC) + timedelta(hours=1),
        WHOOP_SCOPES,
    )
    store.save("vitalii", "main", previous)
    events: list[str] = []
    monkeypatch.setattr(
        "health_agent.whoop.auth_service.validate_registration_target",
        lambda session, profile_id, account: events.append("validated"),
    )

    def register(*args: object) -> SimpleNamespace:
        events.append("registered")
        return SimpleNamespace()

    monkeypatch.setattr(
        "health_agent.whoop.auth_service.register_authorized_connection", register
    )
    monkeypatch.setattr(
        "health_agent.whoop.auth_service._committed_token_generation",
        lambda *args: None,
    )
    session_calls = 0

    @contextmanager
    def failing_session() -> Iterator[object]:
        nonlocal session_calls
        session_calls += 1
        yield object()
        if session_calls == 2:
            events.append("commit")
            raise RuntimeError("database commit failed")

    with pytest.raises(RuntimeError, match="database commit"):
        publish_whoop_authorization(
            failing_session,  # type: ignore[arg-type]
            store,
            UUID("00000000-0000-0000-0000-000000000001"),
            "vitalii",
            "main",
            AuthorizedWhoopAccount(10129, WHOOP_SCOPES, candidate),
        )

    assert events == ["validated", "registered", "commit"]
    assert store.load("vitalii", "main") == previous


def test_publish_keeps_candidate_when_database_commits_before_cleanup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "tokens")
    previous = WhoopToken(
        "old-access",
        "old-refresh",
        datetime.now(UTC) + timedelta(hours=1),
        WHOOP_SCOPES,
    )
    candidate = WhoopToken(
        "new-access",
        "new-refresh",
        datetime.now(UTC) + timedelta(hours=1),
        WHOOP_SCOPES,
    )
    store.save("vitalii", "main", previous)
    connection = SimpleNamespace(token_generation=None)
    committed_generation: UUID | None = None
    session_calls = 0
    monkeypatch.setattr(
        "health_agent.whoop.auth_service.validate_registration_target",
        lambda *args: None,
    )
    monkeypatch.setattr(
        "health_agent.whoop.auth_service.register_authorized_connection",
        lambda *args: connection,
    )
    monkeypatch.setattr(
        "health_agent.whoop.auth_service._committed_token_generation",
        lambda *args: committed_generation,
    )

    @contextmanager
    def post_commit_cleanup_failure() -> Iterator[object]:
        nonlocal committed_generation, session_calls
        session_calls += 1
        current_call = session_calls
        yield object()
        if current_call == 2:
            committed_generation = connection.token_generation
            raise RuntimeError("post-commit session cleanup failed")

    with pytest.raises(RuntimeError, match="post-commit"):
        publish_whoop_authorization(
            post_commit_cleanup_failure,  # type: ignore[arg-type]
            store,
            UUID("00000000-0000-0000-0000-000000000001"),
            "vitalii",
            "main",
            AuthorizedWhoopAccount(10129, WHOOP_SCOPES, candidate),
        )

    assert committed_generation is not None
    assert store.load("vitalii", "main") == candidate
    assert not (store.root / "vitalii" / "main.journal").exists()


def test_invalid_local_target_fails_before_oauth_can_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TokenStore(tmp_path / "tokens")
    monkeypatch.setattr(
        "health_agent.whoop.auth_service.validate_registration_target",
        lambda session, profile_id, account: (_ for _ in ()).throw(
            WhoopRepositoryError("Health profile does not exist")
        ),
    )
    monkeypatch.setattr(
        "health_agent.whoop.auth_service._committed_token_generation",
        lambda *args: None,
    )

    with pytest.raises(WhoopRepositoryError, match="does not exist"):
        validate_whoop_authorization_target(
            object(),  # type: ignore[arg-type]
            store,
            UUID("00000000-0000-0000-0000-000000000099"),
            "missing",
            "main",
        )


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
        wait_for_loopback_callback(
            "http://example.com:8765/callback", timeout_seconds=0
        )
