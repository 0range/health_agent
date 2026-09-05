from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from health_agent.panel.http import PanelApplication
from health_agent.panel.models import (
    ConnectorCard,
    DataCoverage,
    HealthcheckProfile,
    HealthcheckSnapshot,
    ProfilePanel,
    ProfileSummary,
)

FIRST = UUID("10000000-0000-0000-0000-000000000001")
SECOND = UUID("20000000-0000-0000-0000-000000000002")
CHECKED = datetime(2026, 9, 5, 9, 30, tzinfo=UTC)


class SyntheticHealthService:
    """Two isolated profiles; no provider or mutation methods are reachable."""

    def __init__(self) -> None:
        self.healthcheck_reads = 0

    def healthcheck(self) -> HealthcheckSnapshot:
        self.healthcheck_reads += 1
        first = ProfilePanel(
            ProfileSummary(FIRST, "Профиль <A>"),
            (
                ConnectorCard(
                    "whoop",
                    "ready",
                    "Локальная синхронизация завершена.",
                    datetime(2026, 9, 5, 8, tzinfo=UTC),
                ),
            ),
        )
        second = ProfilePanel(
            ProfileSummary(SECOND, "Профиль B"),
            (ConnectorCard("whoop", "not_connected", "WHOOP не подключён."),),
        )
        return HealthcheckSnapshot(
            CHECKED,
            (
                HealthcheckProfile(
                    first,
                    DataCoverage(
                        "available",
                        latest_whoop_date=date(2026, 9, 3),
                        latest_lab_collected_date=date(2026, 8, 20),
                        latest_lab_issued_date=date(2026, 8, 21),
                        latest_received_at=datetime(2026, 8, 25, 10, tzinfo=UTC),
                        pending_extraction_count=2,
                        needs_review_count=3,
                        verified_count=7,
                    ),
                ),
                HealthcheckProfile(second, DataCoverage("empty")),
            ),
        )


def _get(service: SyntheticHealthService, path: str = "/healthcheck"):
    return PanelApplication(service, csrf_token="fixed").handle(  # type: ignore[arg-type]
        "GET", path, {"Host": "127.0.0.1:8766"}, b""
    )


def test_healthcheck_renders_isolated_two_profile_coverage_and_escapes_html() -> None:
    service = SyntheticHealthService()
    response = _get(service)
    html = response.body.decode()

    assert response.status == 200
    assert "Профиль &lt;A&gt;" in html
    assert "Профиль <A>" not in html
    assert "2026-09-03" in html
    assert "2026-09-05T08:00:00" in html
    assert "2026-08-20" in html and "2026-08-21" in html
    assert "Ожидают извлечения: 2" in html
    assert "Требуют проверки: 3" in html
    assert "Проверены: 7" in html
    second = html.split("Профиль B", 1)[1]
    assert "2026-09-03" not in second
    assert "WHOOP не подключён" in second
    assert "Нет данных" in second
    assert service.healthcheck_reads == 1


def test_healthcheck_unknown_is_not_healthy_and_leaks_no_raw_error() -> None:
    service = SyntheticHealthService()
    snapshot = service.healthcheck()
    broken = HealthcheckSnapshot(
        snapshot.checked_at,
        (
            HealthcheckProfile(
                ProfilePanel(
                    ProfileSummary(FIRST, "Безопасный профиль"),
                    (
                        ConnectorCard(
                            "database",
                            "status_unavailable",
                            "Локальный статус недоступен.",
                            error_code="local_status_unavailable",
                        ),
                    ),
                ),
                DataCoverage("unknown"),
            ),
        ),
    )
    service.healthcheck = lambda: broken  # type: ignore[method-assign]

    html = _get(service).body.decode()

    assert "Неизвестно" in html
    assert "Всё работает" not in html
    assert "password=" not in html and "token=" not in html


def test_healthcheck_handles_no_profiles_and_reuses_host_method_checks() -> None:
    service = SyntheticHealthService()
    service.healthcheck = lambda: HealthcheckSnapshot(CHECKED, ())  # type: ignore[method-assign]
    app = PanelApplication(service, csrf_token="fixed")  # type: ignore[arg-type]

    empty = app.handle("GET", "/healthcheck", {"Host": "127.0.0.1:8766"}, b"")
    bad_host = app.handle("GET", "/healthcheck", {"Host": "example.test"}, b"")
    post = app.handle("POST", "/healthcheck", {"Host": "127.0.0.1:8766"}, b"")

    assert empty.status == 200 and "Профилей пока нет" in empty.body.decode()
    assert bad_host.status == 400
    assert post.status == 405 and post.headers["Allow"] == "GET"


def test_healthcheck_database_failure_is_safe_503() -> None:
    service = SyntheticHealthService()

    def fail() -> HealthcheckSnapshot:
        raise RuntimeError("password=secret provider response")

    service.healthcheck = fail  # type: ignore[method-assign]
    response = _get(service)

    assert response.status == 503
    assert "временно недоступно" in response.body.decode()
    assert "password=" not in response.body.decode()
