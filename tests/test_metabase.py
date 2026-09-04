from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner

from health_agent import cli
from health_agent.config import Settings
from health_agent.metabase import MetabaseBootstrapResult, bootstrap_metabase


class FakeMetabase:
    def __init__(self) -> None:
        self.initialized = False
        self.collections: list[dict[str, Any]] = []
        self.databases: list[dict[str, Any]] = []
        self.dashboards: list[dict[str, Any]] = []
        self.cards: list[dict[str, Any]] = []
        self.requests: list[httpx.Request] = []

    def count_named(self, name: str) -> int:
        entities = self.collections + self.databases + self.dashboards + self.cards
        return sum(entity["name"] == name for entity in entities)

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        method = request.method
        payload = self._payload(request)

        if (method, path) == ("GET", "/api/health"):
            return self._response(request, {"status": "ok"})
        if (method, path) == ("GET", "/api/session/properties"):
            token = None if self.initialized else "setup-token"
            return self._response(request, {"setup-token": token})
        if (method, path) == ("POST", "/api/setup"):
            assert payload["token"] == "setup-token"
            assert payload["user"]["email"] == "health-agent@localhost.local"
            self.initialized = True
            return self._response(request, {"id": "first-session"})
        if (method, path) == ("POST", "/api/session"):
            assert payload["username"] == "health-agent@localhost.local"
            return self._response(request, {"id": "later-session"})

        assert request.headers["x-metabase-session"] in {
            "first-session",
            "later-session",
        }
        if (method, path) == ("GET", "/api/collection"):
            return self._response(request, self.collections)
        if (method, path) == ("POST", "/api/collection"):
            return self._create(request, self.collections, payload)
        if (method, path) == ("GET", "/api/database"):
            return self._response(request, {"data": self.databases})
        if (method, path) == ("POST", "/api/database"):
            return self._create(request, self.databases, payload)
        if (method, path) == ("GET", "/api/dashboard"):
            return self._response(request, self.dashboards)
        if (method, path) == ("POST", "/api/dashboard"):
            entity = dict(payload, id=len(self.dashboards) + 1, dashcards=[])
            self.dashboards.append(entity)
            return self._response(request, entity)
        if (method, path) == ("GET", "/api/card"):
            return self._response(request, self.cards)
        if (method, path) == ("POST", "/api/card"):
            return self._create(request, self.cards, payload)
        if method == "GET" and path.startswith("/api/dashboard/"):
            dashboard_id = int(path.rsplit("/", 1)[-1])
            dashboard = next(row for row in self.dashboards if row["id"] == dashboard_id)
            return self._response(request, dashboard)
        if method == "PUT" and path.startswith("/api/dashboard/"):
            dashboard_id = int(path.rsplit("/", 1)[-1])
            dashboard = next(row for row in self.dashboards if row["id"] == dashboard_id)
            dashboard["dashcards"] = payload["dashcards"]
            return self._response(request, dashboard)
        return httpx.Response(404, request=request)

    @staticmethod
    def _payload(request: httpx.Request) -> dict[str, Any]:
        if not request.content:
            return {}
        value = json.loads(request.content)
        assert isinstance(value, dict)
        return value

    @staticmethod
    def _response(request: httpx.Request, payload: Any) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    def _create(
        self,
        request: httpx.Request,
        entities: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> httpx.Response:
        entity = dict(payload, id=len(entities) + 1)
        entities.append(entity)
        return self._response(request, entity)


class FakeConnection:
    def __init__(self, statements: list[str]) -> None:
        self.statements = statements

    def execute(self, statement: object, _parameters: object = None) -> None:
        self.statements.append(str(statement))


class FakeEngine:
    def __init__(self) -> None:
        self.statements: list[str] = []

    @contextmanager
    def begin(self) -> Iterator[FakeConnection]:
        yield FakeConnection(self.statements)


@pytest.fixture
def fake_metabase() -> FakeMetabase:
    return FakeMetabase()


def test_bootstrap_reuses_existing_collection_dashboard_and_card(
    fake_metabase: FakeMetabase,
) -> None:
    settings = Settings(postgres_password="local-secret")
    engine = FakeEngine()
    transport = httpx.MockTransport(fake_metabase.handle)

    first = bootstrap_metabase(
        settings, transport=transport, engine=cast(Engine, engine)
    )
    second = bootstrap_metabase(
        settings, transport=transport, engine=cast(Engine, engine)
    )

    assert first == second
    assert fake_metabase.count_named("Health Agent") == 2
    assert fake_metabase.count_named("Анализы крови") == 1
    assert fake_metabase.count_named("Динамика анализов крови") == 1
    assert len(fake_metabase.dashboards[0]["dashcards"]) == 1

    card = fake_metabase.cards[0]
    assert card["display"] == "line"
    assert "verified_lab_history" in card["dataset_query"]["native"]["query"]
    assert card["visualization_settings"]["graph.dimensions"] == [
        "date",
        "canonical_name",
    ]
    assert card["visualization_settings"]["graph.metrics"] == ["normalized_value"]

    database = fake_metabase.databases[0]
    assert database["details"]["user"] == "health_dashboard"
    assert database["details"]["password"] == "local-secret"

    sql = "\n".join(engine.statements).upper()
    assert "GRANT CONNECT" in sql
    assert "GRANT USAGE" in sql
    assert "GRANT SELECT ON ALL TABLES" in sql
    assert "ALTER DEFAULT PRIVILEGES" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql


def test_metabase_settings_have_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METABASE_URL", raising=False)
    monkeypatch.delenv("METABASE_ADMIN_EMAIL", raising=False)

    settings = Settings()

    assert settings.metabase_url == "http://127.0.0.1:53000"
    assert settings.metabase_admin_email == "health-agent@localhost"


def test_dashboard_setup_prints_only_safe_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "bootstrap_metabase",
        lambda _settings: MetabaseBootstrapResult(
            collection_id=1,
            database_id=2,
            dashboard_id=3,
            card_id=4,
            dashboard_url="http://127.0.0.1:53000/dashboard/3",
        ),
    )

    result = CliRunner().invoke(cli.app, ["dashboard", "setup"])

    assert result.exit_code == 0
    assert result.stdout == (
        "status=ready dashboard_id=3 card_id=4 "
        "url=http://127.0.0.1:53000/dashboard/3\n"
    )
