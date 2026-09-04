from __future__ import annotations

import json

import httplib2
import pytest
from googleapiclient.errors import HttpError
from tenacity import wait_none

from health_agent.google_drive import api
from health_agent.google_drive.types import DriveItem


class FlakyRequest:
    def __init__(self) -> None:
        self.calls = 0

    def execute(self, *, num_retries: int) -> dict[str, str]:
        assert num_retries == 0
        self.calls += 1
        if self.calls < 3:
            response = httplib2.Response({"status": "429"})
            raise HttpError(response, b'{"error":{"errors":[]}}')
        return {"status": "ok"}


def test_retries_rate_limit_without_exposing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = FlakyRequest()
    monkeypatch.setattr(api, "wait_random_exponential", lambda **_: wait_none())
    assert api._execute(request) == {"status": "ok"}
    assert request.calls == 3


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    (
        (403, "userRateLimitExceeded", True),
        (403, "insufficientFilePermissions", False),
        (401, "authError", False),
        (500, "backendError", True),
    ),
)
def test_only_transient_http_errors_are_retryable(
    status: int, reason: str, expected: bool
) -> None:
    response = httplib2.Response({"status": str(status)})
    content = json.dumps({"error": {"errors": [{"reason": reason}]}}).encode()
    assert api._is_retryable(HttpError(response, content)) is expected


def test_transport_errors_are_retryable() -> None:
    assert api._is_retryable(httplib2.HttpLib2Error("temporary")) is True
    assert api._is_retryable(TimeoutError("temporary")) is True


class FakeRequest:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response

    def execute(self, *, num_retries: int) -> dict[str, object]:
        assert num_retries == 0
        return self.response


class FakeResource:
    def __init__(self) -> None:
        self.change_kwargs: dict[str, object] = {}

    def about(self) -> FakeResource:
        return self

    def changes(self) -> FakeResource:
        return self

    def get(self, **kwargs: object) -> FakeRequest:
        assert kwargs == {"fields": "user(permissionId,emailAddress)"}
        return FakeRequest(
            {"user": {"permissionId": "permission-a", "emailAddress": "A@Example.com"}}
        )

    def list(self, **kwargs: object) -> FakeRequest:
        self.change_kwargs = kwargs
        return FakeRequest(
            {
                "changes": [
                    {"changeType": "drive", "removed": False},
                    {
                        "changeType": "file",
                        "fileId": "file-1",
                        "removed": False,
                        "file": {
                            "id": "file-1",
                            "name": "labs.pdf",
                            "mimeType": "application/pdf",
                            "trashed": True,
                        },
                    },
                ],
                "newStartPageToken": "next",
            }
        )


def test_gateway_uses_stable_identity_and_parses_drive_changes_safely() -> None:
    resource = FakeResource()
    gateway = api.GoogleDriveGateway(resource)  # type: ignore[arg-type]

    identity = gateway.account_identity()
    page = gateway.list_changes("cursor")

    assert (identity.permission_id, identity.email) == (
        "permission-a",
        "a@example.com",
    )
    assert page.changes[0].change_type == "drive"
    assert page.changes[0].file_id is None
    assert page.changes[1].item is not None
    assert page.changes[1].item.trashed is True
    assert "changeType" in str(resource.change_kwargs["fields"])


def test_download_yields_more_than_one_bounded_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    written = [b"a" * 3, b"b" * 2]

    class Files:
        def get_media(self, **kwargs: object) -> object:
            return object()

    class Resource:
        def files(self) -> Files:
            return Files()

    class Downloader:
        def __init__(self, buffer: object, request: object, chunksize: int) -> None:
            self.buffer = buffer
            self.index = 0
            assert chunksize == 1024 * 1024

        def next_chunk(self, *, num_retries: int) -> tuple[None, bool]:
            assert num_retries == 0
            self.buffer.write(written[self.index])
            self.index += 1
            return None, self.index == len(written)

    monkeypatch.setattr(api, "MediaIoBaseDownload", Downloader)
    gateway = api.GoogleDriveGateway(Resource())  # type: ignore[arg-type]
    drive_item = DriveItem("file-1", "labs.pdf", "application/pdf", (), can_download=True)

    assert list(gateway.download_chunks(drive_item, None)) == written


def test_list_children_uses_read_only_shared_aware_query_and_parses_fields() -> None:
    calls: dict[str, object] = {}

    class Files:
        def list(self, **kwargs: object) -> FakeRequest:
            calls.update(kwargs)
            return FakeRequest(
                {
                    "files": [
                        {
                            "id": "file-1",
                            "name": "labs.pdf",
                            "mimeType": "application/pdf",
                            "parents": ["root-folder"],
                            "capabilities": {"canDownload": True},
                            "trashed": False,
                        }
                    ]
                }
            )

    class Resource:
        def files(self) -> Files:
            return Files()

    page = api.GoogleDriveGateway(Resource()).list_children(  # type: ignore[arg-type]
        "root-folder", None
    )

    assert page.items[0].file_id == "file-1"
    assert calls["q"] == "'root-folder' in parents and trashed = false"
    assert calls["supportsAllDrives"] is True
    assert calls["includeItemsFromAllDrives"] is True
    assert "trashed" in str(calls["fields"])


def test_gateway_builds_authorized_transport_with_finite_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, object] = {}
    raw_http = object()
    authorized_http = object()
    service = object()

    monkeypatch.setattr(
        api.httplib2,
        "Http",
        lambda *, timeout: calls.update(timeout=timeout) or raw_http,
    )
    monkeypatch.setattr(
        api,
        "AuthorizedHttp",
        lambda credentials, *, http: (
            calls.update(credentials=credentials, http=http) or authorized_http
        ),
    )

    def fake_build(name: str, version: str, **kwargs: object) -> object:
        calls.update(name=name, version=version, build_kwargs=kwargs)
        return service

    monkeypatch.setattr(api, "build", fake_build)
    credentials = object()

    gateway = api.GoogleDriveGateway.from_credentials(  # type: ignore[arg-type]
        credentials, timeout_seconds=17
    )

    assert gateway._service is service
    assert calls["timeout"] == 17
    assert calls["http"] is raw_http
    assert calls["credentials"] is credentials
    assert calls["build_kwargs"] == {
        "http": authorized_http,
        "cache_discovery": False,
    }
