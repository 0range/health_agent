from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from http.client import HTTPConnection
from threading import Thread
from typing import Any, cast
from urllib.parse import urlencode
from uuid import UUID, uuid4

import pytest

from health_agent.google_drive.config import DriveProfile
from health_agent.panel.http import (
    MAX_FORM_BYTES,
    PanelApplication,
    _cli_guidance,
    _safe_destination_url,
    serve_panel,
)
from health_agent.panel.models import ConnectorCard, PanelDestination, ProfileSummary
from health_agent.panel.service import PanelService


@pytest.mark.parametrize("key", ("metabase_labs", "metabase_whoop"))
def test_profile_dashboard_destination_keys_accept_only_local_urls(key: str) -> None:
    local = PanelDestination(key, "Dashboard", "https://localhost:5443/dashboard/7")
    remote = PanelDestination(key, "Dashboard", "https://example.com/dashboard/7")
    assert _safe_destination_url(local) == local.url
    assert _safe_destination_url(remote) is None


@dataclass
class FakeProfiles:
    values: dict[UUID, ProfileSummary]

    def list(self) -> tuple[ProfileSummary, ...]:
        return tuple(sorted(self.values.values(), key=lambda profile: profile.name))

    def get(self, profile_id: UUID) -> ProfileSummary | None:
        return self.values.get(profile_id)

    def create(self, name: str) -> ProfileSummary:
        profile = ProfileSummary(uuid4(), name)
        self.values[profile.id] = profile
        return profile


class FakeReader:
    def __init__(self, card: ConnectorCard | None = None) -> None:
        self.card = card or ConnectorCard(
            "whoop",
            "ready",
            "Локальный статус доступен.",
            datetime(2026, 9, 4, tzinfo=UTC),
            None,
            ("personal",),
        )
        self.connector = self.card.connector

    def cards(self, _profile_id: UUID) -> tuple[ConnectorCard, ...]:
        return (self.card,)


class FakeDrive:
    connector = "drive"

    def __init__(self) -> None:
        self.roots: dict[UUID, tuple[str, ...]] = {}

    def cards(self, profile_id: UUID) -> tuple[ConnectorCard, ...]:
        roots = self.roots.get(profile_id, ())
        return (
            ConnectorCard(
                "drive",
                "needs_authorization" if roots else "not_configured",
                f"Папок Google Drive: {len(roots)}."
                if roots
                else "Папка не настроена.",
            ),
        )

    def folder_ids(self, profile_id: UUID) -> tuple[str, ...]:
        return self.roots.get(profile_id, ())

    def configure(self, profile_id: UUID, folders: list[str]) -> None:
        self.roots[profile_id] = DriveProfile.create(
            str(profile_id), folders
        ).root_folder_ids


def application(
    *, name: str = "Анна", card: ConnectorCard | None = None, port: int = 8766
) -> tuple[PanelApplication, ProfileSummary, FakeDrive]:
    profile = ProfileSummary(uuid4(), name)
    drive = FakeDrive()
    service = PanelService(
        FakeProfiles({profile.id: profile}),
        (FakeReader(card),),
        drive=drive,
        destinations=(
            PanelDestination("metabase", "Дашборды", "http://127.0.0.1:53000"),
            PanelDestination(
                "google_sheets",
                "Google Таблица",
                None,
                "Появится после подключения Google Таблицы",
            ),
        ),
    )
    return (
        PanelApplication(service, csrf_token="test-csrf-token", port=port),
        profile,
        drive,
    )


def text(response) -> str:
    return response.body.decode("utf-8")


def request(
    app: PanelApplication,
    method: str,
    target: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
):
    return app.handle(
        method,
        target,
        {"Host": "127.0.0.1:8766", **(headers or {})},
        body,
    )


def test_profile_page_renders_safe_cards_and_cli_guidance() -> None:
    app, profile, _ = application()

    response = request(app, "GET", f"/profiles/{profile.id}")

    page = text(response)
    assert response.status == 200
    assert "Профиль: Анна" in page
    assert "WHOOP" in page
    assert "Подключено" in page
    assert "Последняя синхронизация" in page
    assert "Всё работает" not in page  # Drive still needs configuration.
    assert "Нужно ваше внимание" in page
    assert '<details class="technical-details">' in page
    assert "health-agent whoop auth" not in page
    assert "Настроить Google Drive" in page
    assert 'href="http://127.0.0.1:53000"' in page
    assert "Появится после подключения Google Таблицы" in page
    assert 'name="csrf_token" value="test-csrf-token"' in page
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"]
    assert response.headers["Referrer-Policy"] == "same-origin"


def test_profile_page_maps_unsynced_and_action_states_to_human_russian() -> None:
    unsynced = ConnectorCard(
        "gmail", "ready", "Аккаунт подключён.", last_success_at=None
    )
    app, profile, _ = application(card=unsynced)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))

    assert "Gmail" in page
    assert "Синхронизация ещё не запускалась" in page
    assert "Нужно действие" in page  # Drive is not configured.
    assert "Технический статус: ready" in page


def test_safe_error_never_renders_as_connected() -> None:
    failing = ConnectorCard(
        "whoop",
        "ready",
        "Последняя сохранённая синхронизация доступна.",
        last_success_at=datetime(2026, 9, 4, tzinfo=UTC),
        error_code="sync_failed",
    )
    app, profile, _ = application(card=failing)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))

    whoop_card = page.split('<h3 id="connector-0">WHOOP</h3>', 1)[1].split(
        "</article>", 1
    )[0]
    assert "Нужно действие" in whoop_card
    assert "Подключено" not in whoop_card


@pytest.mark.parametrize(
    ("card", "expected_action", "forbidden_action"),
    (
        (
            ConnectorCard(
                "whoop",
                "reauth_required",
                "Аккаунт требует внимания.",
                error_code="reauth_required",
            ),
            "Подключите или переподключите WHOOP.",
            "Переподключение не требуется",
        ),
        (
            ConnectorCard(
                "whoop",
                "reauth_required",
                "Аккаунт требует внимания.",
                error_code="sync_failed",
            ),
            "Подключите или переподключите WHOOP.",
            "Повторите синхронизацию позже",
        ),
        (
            ConnectorCard(
                "whoop",
                "ready",
                "Аккаунт требует внимания.",
                error_code="rate_limited",
            ),
            "Подождите следующей автоматической попытки; переподключение не требуется.",
            "Подключите или переподключите WHOOP.",
        ),
        (
            ConnectorCard(
                "whoop",
                "ready",
                "Аккаунт требует внимания.",
                error_code="sync_failed",
            ),
            "Повторите синхронизацию позже. Если ошибка повторится, откройте подробности.",
            "Подключите или переподключите WHOOP.",
        ),
        (
            ConnectorCard(
                "gmail",
                "ready",
                "Аккаунт требует внимания.",
                error_code="AttachmentPreparationError",
            ),
            "Повторите синхронизацию позже. Если ошибка повторится, откройте подробности.",
            "Подключите или переподключите Gmail.",
        ),
    ),
)
def test_visible_remediation_matches_failure_kind(
    card: ConnectorCard, expected_action: str, forbidden_action: str
) -> None:
    app, profile, _ = application(card=card)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))
    connector_label = "WHOOP" if card.connector == "whoop" else "Gmail"
    connector_card = page.split(f">{connector_label}</h3>", 1)[1].split(
        "</article>", 1
    )[0]

    assert expected_action in connector_card
    assert forbidden_action not in connector_card


def test_configured_card_with_previous_success_is_not_marked_never_synced() -> None:
    configured = ConnectorCard(
        "gmail",
        "configured",
        "Аккаунт настроен.",
        last_success_at=datetime(2026, 9, 4, tzinfo=UTC),
    )
    app, profile, _ = application(card=configured)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))
    gmail_card = page.split(">Gmail</h3>", 1)[1].split("</article>", 1)[0]

    assert "Подключено" in gmail_card
    assert "Последняя синхронизация" in gmail_card
    assert "Синхронизация ещё не запускалась" not in gmail_card
    assert "Успешной синхронизации ещё не было" not in gmail_card


def test_profile_page_has_semantic_sections_and_collapsed_identifiers() -> None:
    card = ConnectorCard(
        "whoop",
        "reauth_required",
        "Нужно снова подключить аккаунт.",
        error_code="reauth_required",
        account_ids=("personal<unsafe>",),
    )
    app, profile, _ = application(card=card)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))

    assert page.count("<h1") == 1
    assert 'aria-labelledby="system-status"' in page
    assert 'aria-labelledby="destinations"' in page
    assert '<details class="profile-details">' in page
    assert f"ID профиля: {profile.id}" in page
    assert "personal&lt;unsafe&gt;" in page
    assert "Код ошибки: reauth_required" in page
    assert "personal<unsafe>" not in page


def test_destination_renderer_rejects_unsafe_urls_and_escapes_copy() -> None:
    profile = ProfileSummary(uuid4(), "Анна")
    service = PanelService(
        FakeProfiles({profile.id: profile}),
        destinations=(
            PanelDestination(
                "metabase",
                '<img src=x onerror="boom">',
                "https://attacker.example/?token=secret",
                '<script>alert("fallback")</script>',
            ),
        ),
    )
    app = PanelApplication(service, csrf_token="test-csrf-token", port=8766)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))

    assert "attacker.example" not in page
    assert "token=secret" not in page
    assert "<script>" not in page
    assert "&lt;img src=x onerror=&quot;boom&quot;&gt;" in page
    assert "&lt;script&gt;alert(&quot;fallback&quot;)&lt;/script&gt;" in page


def test_destination_renderer_accepts_only_verified_google_sheet_shape() -> None:
    profile = ProfileSummary(uuid4(), "Анна")
    sheet_url = "https://docs.google.com/spreadsheets/d/verified-sheet-id-123456/edit"
    service = PanelService(
        FakeProfiles({profile.id: profile}),
        destinations=(PanelDestination("google_sheets", "Google Таблица", sheet_url),),
    )
    app = PanelApplication(service, csrf_token="test-csrf-token", port=8766)

    page = text(request(app, "GET", f"/profiles/{profile.id}"))

    assert f'href="{sheet_url}"' in page
    assert 'rel="noreferrer"' in page
    assert "Открыть в Google" in page
    assert "Открыть локально" not in page


def test_profile_page_renders_telegram_status_with_the_profile_option() -> None:
    app, profile, _ = application(
        card=ConnectorCard("telegram", "not_bound", "Профиль не привязан.")
    )

    response = request(app, "GET", f"/profiles/{profile.id}")

    assert f"health-agent telegram bind {profile.id} &lt;telegram-user-id&gt;" in text(
        response
    )


def test_html_escapes_profile_and_connector_values() -> None:
    app, profile, _ = application(name='<img src=x onerror="boom">')

    response = request(app, "GET", f"/profiles/{profile.id}")

    page = text(response)
    assert '<img src=x onerror="boom">' not in page
    assert "&lt;img src=x onerror=&quot;boom&quot;&gt;" in page


def test_home_page_lists_profiles_and_includes_csrf_protected_create_form() -> None:
    app, profile, _ = application()

    response = request(app, "GET", "/")

    page = text(response)
    assert response.status == 200
    assert f"/profiles/{profile.id}" in page
    assert 'aria-label="Имя нового профиля"' in page
    assert 'name="csrf_token" value="test-csrf-token"' in page


def test_missing_or_invalid_profile_is_not_found() -> None:
    app, _, _ = application()

    assert request(app, "GET", f"/profiles/{uuid4()}").status == 404
    assert request(app, "GET", "/profiles/not-a-uuid").status == 404
    assert request(app, "GET", f"/profiles/{uuid4().hex}").status == 404


def test_rejects_oversize_post_body_before_form_parsing() -> None:
    app, _, _ = application()

    response = request(app, "POST", "/profiles", body=b"x" * (MAX_FORM_BYTES + 1))

    assert response.status == 413


def test_rejects_unsupported_methods_and_routes() -> None:
    app, _, _ = application()

    response = request(app, "PUT", "/")
    assert response.status == 405
    assert response.headers["Allow"] == "GET"
    assert request(app, "GET", "/profiles").status == 404
    assert request(app, "GET", "/?ignored").status == 404
    assert request(app, "POST", "/unknown").status == 404


def test_post_creates_profile_only_with_csrf_and_same_origin() -> None:
    app, _, _ = application()
    body = b"name=%D0%92%D0%B8%D0%BA%D1%82%D0%BE%D1%80&csrf_token=test-csrf-token"

    response = request(
        app,
        "POST",
        "/profiles",
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "http://127.0.0.1:8766",
        },
        body,
    )

    assert response.status == 303
    assert response.headers["Location"] == "/"
    assert "Виктор" in text(request(app, "GET", "/"))


def test_post_configures_selected_profiles_drive_folders_and_shows_success() -> None:
    app, profile, drive = application()
    first = "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB"
    second = "2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    body = urlencode(
        {
            "folders": (f"https://drive.google.com/drive/folders/{first}\n{second}"),
            "csrf_token": "test-csrf-token",
        }
    ).encode()

    response = request(
        app,
        "POST",
        f"/profiles/{profile.id}/drive",
        {"Origin": "http://127.0.0.1:8766"},
        body,
    )

    assert response.status == 303
    assert response.headers["Location"] == f"/profiles/{profile.id}/drive-saved"
    assert drive.roots[profile.id] == (first, second)
    success = request(app, "GET", response.headers["Location"])
    assert success.status == 200
    assert "Настройка Google Drive сохранена." in text(success)
    assert first in text(success)
    assert second in text(success)


@pytest.mark.parametrize(
    "folders",
    (
        "https://evil.example/drive/folders/1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
        "javascript:alert(1)",
        '<script>alert("boom")</script>',
        "   ",
    ),
)
def test_drive_form_rejects_invalid_or_hostile_folder_values(folders: str) -> None:
    app, profile, drive = application()
    body = urlencode({"folders": folders, "csrf_token": "test-csrf-token"}).encode()

    response = request(
        app,
        "POST",
        f"/profiles/{profile.id}/drive",
        {"Origin": "http://127.0.0.1:8766"},
        body,
    )

    page = text(response)
    assert response.status == 400
    assert "Проверьте ссылки на папки Google Drive." in page
    assert profile.id not in drive.roots
    assert "<script>" not in page
    assert "Traceback" not in page


def test_drive_form_rejects_missing_csrf_cross_origin_duplicates_and_oversize() -> None:
    app, profile, drive = application()
    path = f"/profiles/{profile.id}/drive"
    valid = urlencode(
        {
            "folders": "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
            "csrf_token": "test-csrf-token",
        }
    ).encode()

    assert request(app, "POST", path, body=valid).status == 403
    assert (
        request(
            app,
            "POST",
            path,
            {"Origin": "https://attacker.example"},
            valid,
        ).status
        == 403
    )
    duplicate = valid + b"&folders=2g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsC"
    assert (
        request(
            app,
            "POST",
            path,
            {"Origin": "http://127.0.0.1:8766"},
            duplicate,
        ).status
        == 400
    )
    assert (
        request(
            app,
            "POST",
            path,
            {"Origin": "http://127.0.0.1:8766"},
            b"x" * (MAX_FORM_BYTES + 1),
        ).status
        == 413
    )
    assert profile.id not in drive.roots


def test_drive_form_rejects_unknown_profile_without_writing_configuration() -> None:
    app, _, drive = application()
    unknown = uuid4()
    body = urlencode(
        {
            "folders": "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
            "csrf_token": "test-csrf-token",
        }
    ).encode()

    response = request(
        app,
        "POST",
        f"/profiles/{unknown}/drive",
        {"Origin": "http://127.0.0.1:8766"},
        body,
    )

    assert response.status == 404
    assert unknown not in drive.roots


def test_drive_form_hides_local_storage_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, profile, drive = application()

    def fail_safely(_profile_id: UUID, _folders: list[str]) -> None:
        raise RuntimeError("refresh_token=secret MRI-result.pdf")

    monkeypatch.setattr(drive, "configure", fail_safely)
    body = urlencode(
        {
            "folders": "1g9ndH8Ue8XWJ6pjKSj4YPqLeGXw4ycsB",
            "csrf_token": "test-csrf-token",
        }
    ).encode()

    response = request(
        app,
        "POST",
        f"/profiles/{profile.id}/drive",
        {"Origin": "http://127.0.0.1:8766"},
        body,
    )

    page = text(response)
    assert response.status == 500
    assert "Не удалось сохранить настройку Google Drive." in page
    assert "refresh_token" not in page
    assert "MRI-result.pdf" not in page
    assert "Traceback" not in page


def test_post_rejects_missing_csrf_or_cross_origin() -> None:
    app, _, _ = application()
    valid_body = b"name=Viktor&csrf_token=test-csrf-token"

    assert request(app, "POST", "/profiles", body=b"name=Viktor").status == 403
    assert request(app, "POST", "/profiles", body=valid_body).status == 403
    assert (
        request(
            app,
            "POST",
            "/profiles",
            {"Origin": "null"},
            valid_body,
        ).status
        == 403
    )
    assert (
        request(
            app,
            "POST",
            "/profiles",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "http://127.0.0.1:8767",
            },
            valid_body,
        ).status
        == 403
    )
    assert (
        request(
            app,
            "POST",
            "/profiles",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://example.test",
            },
            valid_body,
        ).status
        == 403
    )


def test_page_does_not_render_secrets_or_medical_fields() -> None:
    app, _, _ = application()

    page = text(request(app, "GET", "/"))

    for forbidden in ("access_token", "refresh_token", "source_value"):
        assert forbidden not in page
    # Shared CSS class names are not medical data or overview content.
    assert "medical" not in page.split("</style>", 1)[-1]


def test_server_refuses_non_loopback_hosts() -> None:
    app, _, _ = application()

    with pytest.raises(ValueError, match="127.0.0.1"):
        serve_panel(app._service, host="0.0.0.0", port=0)


@pytest.mark.parametrize("method", ("GET", "POST"))
def test_application_rejects_hostile_host_before_route_dispatch(method: str) -> None:
    app, _, _ = application()
    hostile = "attacker.example:8766"

    response = app.handle(
        method,
        "/" if method == "GET" else "/profiles",
        {"Host": hostile, "Origin": "http://127.0.0.1:8766"},
        b"name=Viktor&csrf_token=test-csrf-token",
    )

    assert response.status == 400
    assert hostile not in text(response)


def test_default_http_port_uses_browser_canonical_host_and_origin() -> None:
    app, _, _ = application(port=80)
    body = b"name=Viktor&csrf_token=test-csrf-token"

    response = app.handle(
        "POST",
        "/profiles",
        {"Host": "127.0.0.1", "Origin": "http://127.0.0.1"},
        body,
    )

    assert response.status == 303
    assert app.handle("GET", "/", {"Host": "127.0.0.1"}, b"").status == 200
    assert (
        app.handle(
            "POST",
            "/profiles",
            {"Host": "127.0.0.1:80", "Origin": "http://127.0.0.1:80"},
            body,
        ).status
        == 400
    )
    assert (
        app.handle(
            "POST",
            "/profiles",
            {"Host": "127.0.0.1", "Origin": "http://127.0.0.1:80"},
            body,
        ).status
        == 403
    )


@pytest.mark.parametrize(
    ("card", "expected", "forbidden"),
    (
        (
            ConnectorCard("whoop", "not_connected", "", account_ids=()),
            "health-agent whoop auth --profile-id {profile_id} --account <account>",
            "--account main",
        ),
        (
            ConnectorCard("whoop", "ready", "", account_ids=("personal",)),
            "действий не требуется",
            "health-agent",
        ),
        (
            ConnectorCard(
                "whoop",
                "reauth_required",
                "",
                error_code="reauth_required",
                account_ids=("personal",),
            ),
            "health-agent whoop auth --profile-id {profile_id} --account personal",
            "--account main",
        ),
        (
            ConnectorCard(
                "whoop",
                "ready",
                "",
                error_code="sync_failed",
                account_ids=("personal",),
            ),
            "health-agent whoop status --profile-id {profile_id} --account personal",
            "действий не требуется",
        ),
        (
            ConnectorCard(
                "whoop",
                "reauth_required",
                "",
                error_code="reauth_required",
                account_ids=("first", "second"),
            ),
            "health-agent whoop status --profile-id {profile_id} --account <account>",
            "--account main",
        ),
        (
            ConnectorCard("gmail", "not_configured", "", account_ids=()),
            "health-agent gmail configure {profile_id} <account-id>",
            "personal",
        ),
        (
            ConnectorCard("gmail", "ready", "", account_ids=("personal",)),
            "действий не требуется",
            "health-agent",
        ),
        (
            ConnectorCard(
                "gmail",
                "reauth_required",
                "",
                error_code="OAuthRequired",
                account_ids=("personal",),
            ),
            "health-agent gmail auth {profile_id} personal",
            "<account-id>",
        ),
        (
            ConnectorCard(
                "gmail",
                "reauth_required",
                "",
                error_code="OAuthRequired",
                account_ids=("first", "second"),
            ),
            "health-agent gmail status {profile_id}",
            "personal",
        ),
        (
            ConnectorCard("drive", "not_configured", ""),
            "укажите папку Google Drive в форме ниже",
            "health-agent drive auth",
        ),
        (
            ConnectorCard("drive", "needs_authorization", ""),
            "health-agent drive auth {profile_id}",
            "пока недоступна",
        ),
    ),
)
def test_cli_guidance_state_action_matrix(
    card: ConnectorCard, expected: str, forbidden: str
) -> None:
    profile_id = uuid4()

    guidance = _cli_guidance(card, profile_id)

    assert expected.format(profile_id=profile_id) in guidance
    assert forbidden not in guidance


def _live_request(
    server,
    method: str,
    target: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
):
    worker = Thread(target=server.handle_request)
    worker.start()
    connection = HTTPConnection("127.0.0.1", server.server_address[1], timeout=2)
    connection.request(method, target, body=body, headers=headers or {})
    response = connection.getresponse()
    response_body = response.read()
    worker.join(timeout=2)
    connection.close()
    return response, response_body


@pytest.mark.parametrize("method", ("GET", "POST"))
def test_ephemeral_server_rejects_hostile_host_for_get_and_post(method: str) -> None:
    app, _, _ = application()
    server = serve_panel(app._service, host="127.0.0.1", port=0)
    hostile = "attacker.example"
    try:
        response, response_body = _live_request(
            server,
            method,
            "/" if method == "GET" else "/profiles",
            headers={
                "Host": hostile,
                "Origin": f"http://127.0.0.1:{server.server_address[1]}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            body=(
                b"name=Viktor&csrf_token=not-the-token" if method == "POST" else None
            ),
        )
    finally:
        server.server_close()

    assert response.status == 400
    assert hostile.encode() not in response_body


def test_ephemeral_server_accepts_its_actual_bound_host_and_origin() -> None:
    app, _, _ = application()
    server = serve_panel(app._service, host="127.0.0.1", port=0)
    port = server.server_address[1]
    live_application = cast(Any, server.RequestHandlerClass).application
    body = f"name=Viktor&csrf_token={live_application._csrf_token}".encode()
    try:
        get_response, _ = _live_request(server, "GET", "/")
        post_response, _ = _live_request(
            server,
            "POST",
            "/profiles",
            headers={"Origin": f"http://127.0.0.1:{port}"},
            body=body,
        )
    finally:
        server.server_close()

    assert get_response.status == 200
    assert post_response.status == 303


def test_server_adapter_routes_unsupported_methods_to_application() -> None:
    app, _, _ = application()
    server = serve_panel(app._service, host="127.0.0.1", port=0)
    try:
        response, _ = _live_request(server, "PUT", "/")
    finally:
        server.server_close()
    assert response.status == 405
    assert response.getheader("Allow") == "GET"
