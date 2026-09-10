import json
import time

import httpx
import pytest

from health_agent.qingping.client import QingpingClient, QingpingError


def token():
    return httpx.Response(200, json={"access_token": "synthetic", "expires_in": 7200})


def test_pages_and_reauthorizes_once_without_changing_request_window():
    requests = []
    token_calls = 0
    rejected = False

    def respond(request):
        nonlocal token_calls, rejected
        if request.url.host == "oauth.cleargrass.com":
            token_calls += 1
            assert b"grant_type=client_credentials" in request.content
            return token()
        requests.append(request)
        if not rejected:
            rejected = True
            return httpx.Response(401)
        offset = int(request.url.params["offset"])
        return httpx.Response(200, json={"total": 3, "data": [{"id": offset}]})

    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        rows = QingpingClient(http, "key", "secret").history("AABBCCDDEEFF", 100, 200)
    assert rows == [{"id": 0}, {"id": 1}, {"id": 2}]
    assert token_calls == 2
    assert {r.url.params["start_time"] for r in requests} == {"100"}
    assert {r.url.params["end_time"] for r in requests} == {"200"}
    assert len({r.url.params["timestamp"] for r in requests}) == len(requests)


@pytest.mark.parametrize(
    "response,code",
    [
        (
            httpx.Response(503, text="sensitive upstream error"),
            "qingping_api_unavailable",
        ),
        (httpx.Response(429), "qingping_api_unavailable"),
        (
            httpx.Response(200, json={"total": 2, "data": []}),
            "qingping_incomplete_history",
        ),
        (
            httpx.Response(200, json={"error": "no data access"}),
            "qingping_invalid_page",
        ),
    ],
)
def test_failures_are_explicit_and_content_free(response, code):
    def respond(request):
        return token() if request.url.host == "oauth.cleargrass.com" else response

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        pytest.raises(QingpingError, match=f"^{code}$"),
    ):
        QingpingClient(http, "key", "secret").history("AABBCCDDEEFF", 100, 200)


def test_private_token_cache_survives_restart_and_renews_expired_token(tmp_path):
    token_calls = 0

    def respond(request):
        nonlocal token_calls
        if request.url.host == "oauth.cleargrass.com":
            token_calls += 1
            return token()
        return httpx.Response(200, json={"total": 0, "devices": []})

    path = tmp_path / "token.json"
    with httpx.Client(transport=httpx.MockTransport(respond)) as http:
        for _ in range(2):
            QingpingClient(http, "key", "secret", path).devices()
        assert token_calls == 1
        assert path.stat().st_mode & 0o777 == 0o600
        cache = json.loads(path.read_text())
        cache["expires_at"] = time.time() - 1
        path.write_text(json.dumps(cache))
        QingpingClient(http, "key", "secret", path).devices()
        assert token_calls == 2
        QingpingClient(http, "different-key", "secret", path).devices()
        assert token_calls == 3


def test_temporary_oauth_failure_does_not_erase_cached_credentials(tmp_path):
    path = tmp_path / "token.json"
    path.write_text(
        json.dumps({"identity": "old", "access_token": "old", "expires_at": 0})
    )
    path.chmod(0o600)
    previous = path.read_bytes()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(503))
        ) as http,
        pytest.raises(QingpingError, match="qingping_token_unavailable"),
    ):
        QingpingClient(http, "key", "secret", path).devices()
    assert path.read_bytes() == previous
