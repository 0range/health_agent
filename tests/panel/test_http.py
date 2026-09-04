from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from health_agent.panel.http import MAX_FORM_BYTES, PanelApplication, serve_panel
from health_agent.panel.models import ConnectorCard, ProfileSummary
from health_agent.panel.service import PanelService


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
    connector = "whoop"

    def cards(self, _profile_id: UUID) -> tuple[ConnectorCard, ...]:
        return (
            ConnectorCard(
                "whoop",
                "ready",
                "Local status is safe.",
                datetime(2026, 9, 4, tzinfo=UTC),
                None,
            ),
        )


def application(*, name: str = "Анна") -> tuple[PanelApplication, ProfileSummary]:
    profile = ProfileSummary(uuid4(), name)
    service = PanelService(FakeProfiles({profile.id: profile}), (FakeReader(),))
    return PanelApplication(service, csrf_token="test-csrf-token"), profile


def text(response) -> str:
    return response.body.decode("utf-8")


def test_profile_page_renders_safe_cards_and_cli_guidance() -> None:
    app, profile = application()

    response = app.handle("GET", f"/profiles/{profile.id}", {}, b"")

    page = text(response)
    assert response.status == 200
    assert "Профиль: Анна" in page
    assert "WHOOP" in page
    assert "Готово" in page
    assert "Последняя успешная операция" in page
    assert "health-agent whoop auth" in page
    assert "<button" not in page
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Content-Security-Policy"]


def test_html_escapes_profile_and_connector_values() -> None:
    app, profile = application(name='<img src=x onerror="boom">')

    response = app.handle("GET", f"/profiles/{profile.id}", {}, b"")

    page = text(response)
    assert '<img src=x onerror="boom">' not in page
    assert "&lt;img src=x onerror=&quot;boom&quot;&gt;" in page


def test_home_page_lists_profiles_and_includes_csrf_protected_create_form() -> None:
    app, profile = application()

    response = app.handle("GET", "/", {}, b"")

    page = text(response)
    assert response.status == 200
    assert f'/profiles/{profile.id}' in page
    assert 'aria-label="Имя нового профиля"' in page
    assert 'name="csrf_token" value="test-csrf-token"' in page


def test_missing_or_invalid_profile_is_not_found() -> None:
    app, _ = application()

    assert app.handle("GET", f"/profiles/{uuid4()}", {}, b"").status == 404
    assert app.handle("GET", "/profiles/not-a-uuid", {}, b"").status == 404
    assert app.handle("GET", f"/profiles/{uuid4().hex}", {}, b"").status == 404


def test_rejects_oversize_post_body_before_form_parsing() -> None:
    app, _ = application()

    response = app.handle("POST", "/profiles", {}, b"x" * (MAX_FORM_BYTES + 1))

    assert response.status == 413


def test_rejects_unsupported_methods_and_routes() -> None:
    app, _ = application()

    response = app.handle("PUT", "/", {}, b"")
    assert response.status == 405
    assert response.headers["Allow"] == "GET"
    assert app.handle("GET", "/profiles", {}, b"").status == 404
    assert app.handle("GET", "/?ignored", {}, b"").status == 404
    assert app.handle("POST", "/unknown", {}, b"").status == 404


def test_post_creates_profile_only_with_csrf_and_same_origin() -> None:
    app, _ = application()
    body = b"name=%D0%92%D0%B8%D0%BA%D1%82%D0%BE%D1%80&csrf_token=test-csrf-token"

    response = app.handle(
        "POST",
        "/profiles",
        {"Content-Type": "application/x-www-form-urlencoded", "Origin": "http://127.0.0.1:8766"},
        body,
    )

    assert response.status == 303
    assert response.headers["Location"] == "/"
    assert "Виктор" in text(app.handle("GET", "/", {}, b""))


def test_post_rejects_missing_csrf_or_cross_origin() -> None:
    app, _ = application()
    valid_body = b"name=Viktor&csrf_token=test-csrf-token"

    assert app.handle("POST", "/profiles", {}, b"name=Viktor").status == 403
    assert (
        app.handle(
            "POST",
            "/profiles",
            {"Content-Type": "application/x-www-form-urlencoded", "Origin": "https://example.test"},
            valid_body,
        ).status
        == 403
    )


def test_page_does_not_render_secrets_or_medical_fields() -> None:
    app, _ = application()

    page = text(app.handle("GET", "/", {}, b""))

    for forbidden in ("access_token", "refresh_token", "source_value", "medical"):
        assert forbidden not in page


def test_server_refuses_non_loopback_hosts() -> None:
    app, _ = application()

    with pytest.raises(ValueError, match="127.0.0.1"):
        serve_panel(app._service, host="0.0.0.0", port=0)
