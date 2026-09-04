from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast

import httpx
import pytest
from conftest import DisposablePostgres
from pydantic import ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from typer.testing import CliRunner

from health_agent import cli
from health_agent.config import Settings
from health_agent.metabase import (
    LAB_HISTORY_QUERY,
    MetabaseBootstrapResult,
    bootstrap_metabase,
    ensure_dashboard_reader,
)


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
        if method == "PUT" and path.startswith("/api/collection/"):
            return self._update(request, self.collections, payload)
        if method == "PUT" and path.startswith("/api/database/"):
            return self._update(request, self.databases, payload)
        if method == "PUT" and path.startswith("/api/card/"):
            return self._update(request, self.cards, payload)
        if method == "GET" and path.startswith("/api/dashboard/"):
            dashboard_id = int(path.rsplit("/", 1)[-1])
            dashboard = next(row for row in self.dashboards if row["id"] == dashboard_id)
            return self._response(request, dashboard)
        if method == "PUT" and path.startswith("/api/dashboard/"):
            dashboard_id = int(path.rsplit("/", 1)[-1])
            dashboard = next(row for row in self.dashboards if row["id"] == dashboard_id)
            dashboard.update(payload)
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

    def _update(
        self,
        request: httpx.Request,
        entities: list[dict[str, Any]],
        payload: dict[str, Any],
    ) -> httpx.Response:
        entity_id = int(request.url.path.rsplit("/", 1)[-1])
        entity = next(row for row in entities if row["id"] == entity_id)
        entity.update(payload)
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
    assert card["dataset_query"]["native"]["query"] == LAB_HISTORY_QUERY
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
    assert "PG_AUTH_MEMBERS" in sql
    assert "PG_DEFAULT_ACL" in sql
    assert "SELECT DATNAME FROM PG_DATABASE" in sql
    assert "NOINHERIT" in sql
    assert "OWNS DATABASE OBJECTS" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql


def test_metabase_settings_have_local_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("METABASE_URL", raising=False)
    monkeypatch.delenv("METABASE_ADMIN_EMAIL", raising=False)

    settings = Settings()

    assert settings.metabase_url == "http://127.0.0.1:53000"
    assert settings.metabase_admin_email == "health-agent@localhost"
    assert settings.effective_metabase_admin_email == "health-agent@localhost.local"


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com:53000",
        "https://example.com:53000",
        "ftp://127.0.0.1:53000",
        "http://user:secret@127.0.0.1:53000",
        "http://127.0.0.1:53000?token=secret",
        "http://127.0.0.1:53000/#secret",
    ],
)
def test_metabase_url_rejects_non_local_or_printable_credentials(url: str) -> None:
    with pytest.raises(ValidationError):
        Settings(metabase_url=url)


def test_explicit_metabase_email_must_already_be_api_valid() -> None:
    settings = Settings(metabase_admin_email="owner@example.test")
    assert settings.effective_metabase_admin_email == "owner@example.test"

    with pytest.raises(ValidationError):
        Settings(metabase_admin_email="someone@internal")


def test_bootstrap_repairs_drifted_same_named_objects(
    fake_metabase: FakeMetabase,
) -> None:
    fake_metabase.initialized = True
    fake_metabase.collections.append(
        {"id": 7, "name": "Health Agent", "parent_id": 99, "location": "/99/"}
    )
    fake_metabase.databases.append(
        {
            "id": 8,
            "name": "Health Agent",
            "engine": "h2",
            "details": {
                "host": "wrong",
                "port": 1,
                "dbname": "wrong",
                "user": "writer",
                "ssl": True,
            },
        }
    )
    fake_metabase.dashboards.append(
        {"id": 9, "name": "Анализы крови", "collection_id": 99, "dashcards": []}
    )
    fake_metabase.cards.append(
        {
            "id": 10,
            "name": "Динамика анализов крови",
            "collection_id": 99,
            "display": "table",
            "dataset_query": {
                "database": 999,
                "type": "native",
                "native": {"query": "SELECT secret FROM private_data"},
            },
            "visualization_settings": {},
        }
    )
    engine = cast(Engine, FakeEngine())

    result = bootstrap_metabase(
        Settings(postgres_password="local-secret"),
        transport=httpx.MockTransport(fake_metabase.handle),
        engine=engine,
    )

    assert result == MetabaseBootstrapResult(
        collection_id=7,
        database_id=8,
        dashboard_id=9,
        card_id=10,
        dashboard_url="http://127.0.0.1:53000/dashboard/9",
        admin_email="health-agent@localhost.local",
    )
    assert fake_metabase.collections[0]["parent_id"] is None
    assert fake_metabase.databases[0]["engine"] == "postgres"
    assert fake_metabase.databases[0]["details"]["user"] == "health_dashboard"
    assert fake_metabase.dashboards[0]["collection_id"] == 7
    assert fake_metabase.cards[0]["collection_id"] == 7
    assert fake_metabase.cards[0]["display"] == "line"
    assert fake_metabase.cards[0]["dataset_query"]["native"]["query"] == LAB_HISTORY_QUERY
    assert fake_metabase.cards[0]["visualization_settings"] == {
        "graph.dimensions": ["date", "canonical_name"],
        "graph.metrics": ["normalized_value"],
    }
    assert fake_metabase.dashboards[0]["dashcards"][0]["card_id"] == 10


def test_dashboard_reader_repairs_existing_privileges_and_membership(
    disposable_postgres: DisposablePostgres,
) -> None:
    settings = disposable_postgres.settings
    engine = disposable_postgres.engine
    ensure_dashboard_reader(settings, engine=engine)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "DO $$ BEGIN CREATE ROLE health_dashboard_test_parent CREATEDB; "
                    "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                )
            )
            connection.execute(
                text("GRANT health_dashboard_test_parent TO health_dashboard")
            )
            connection.execute(text("ALTER ROLE health_dashboard INHERIT CREATEDB"))
            connection.execute(
                text("GRANT INSERT, UPDATE ON lab_observations TO health_dashboard")
            )
            connection.execute(
                text(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE health_agent IN SCHEMA public "
                    "GRANT INSERT ON TABLES TO health_dashboard"
                )
            )

        ensure_dashboard_reader(settings, engine=engine)

        with engine.connect() as connection:
            role_state = connection.execute(
                text(
                    "SELECT rolcreatedb, rolinherit FROM pg_roles "
                    "WHERE rolname = 'health_dashboard'"
                )
            ).one()
            memberships = connection.scalar(
                text(
                    "SELECT count(*) FROM pg_auth_members membership "
                    "JOIN pg_roles child ON child.oid = membership.member "
                    "WHERE child.rolname = 'health_dashboard'"
                )
            )
            writes = connection.execute(
                text(
                    "SELECT "
                    "has_table_privilege('health_dashboard', 'lab_observations', 'INSERT'), "
                    "has_table_privilege('health_dashboard', 'lab_observations', 'UPDATE')"
                )
            ).one()
            default_privileges = connection.execute(
                text(
                    "SELECT DISTINCT privilege.privilege_type::text "
                    "FROM pg_default_acl defaults "
                    "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege "
                    "JOIN pg_roles grantee ON grantee.oid = privilege.grantee "
                    "WHERE grantee.rolname = 'health_dashboard' "
                    "ORDER BY privilege.privilege_type::text"
                )
            ).scalars().all()
            database_grants = connection.execute(
                text(
                    "SELECT database_object.datname, privilege.privilege_type::text "
                    "FROM pg_database database_object "
                    "CROSS JOIN LATERAL aclexplode(database_object.datacl) privilege "
                    "JOIN pg_roles grantee ON grantee.oid = privilege.grantee "
                    "WHERE grantee.rolname = 'health_dashboard' ORDER BY 1, 2"
                )
            ).all()
            schema_grants = connection.execute(
                text(
                    "SELECT namespace.nspname, privilege.privilege_type::text "
                    "FROM pg_namespace namespace "
                    "CROSS JOIN LATERAL aclexplode(namespace.nspacl) privilege "
                    "JOIN pg_roles grantee ON grantee.oid = privilege.grantee "
                    "WHERE grantee.rolname = 'health_dashboard' ORDER BY 1, 2"
                )
            ).all()
            relation_grants = connection.execute(
                text(
                    "SELECT namespace.nspname, privilege.privilege_type::text "
                    "FROM pg_class relation_object "
                    "JOIN pg_namespace namespace ON namespace.oid = relation_object.relnamespace "
                    "CROSS JOIN LATERAL aclexplode(relation_object.relacl) privilege "
                    "JOIN pg_roles grantee ON grantee.oid = privilege.grantee "
                    "WHERE grantee.rolname = 'health_dashboard' ORDER BY 1, 2"
                )
            ).all()

        assert role_state == (False, False)
        assert memberships == 0
        assert writes == (False, False)
        assert default_privileges == ["SELECT"]
        assert database_grants == [(settings.postgres_database, "CONNECT")]
        assert schema_grants == [("public", "USAGE")]
        assert relation_grants
        assert all(grant == ("public", "SELECT") for grant in relation_grants)
    finally:
        ensure_dashboard_reader(settings, engine=engine)
        with engine.begin() as connection:
            connection.execute(text("DROP ROLE IF EXISTS health_dashboard_test_parent"))


def test_dashboard_reader_fails_closed_when_role_owns_an_object(
    disposable_postgres: DisposablePostgres,
) -> None:
    settings = disposable_postgres.settings
    engine = disposable_postgres.engine
    ensure_dashboard_reader(settings, engine=engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE IF EXISTS health_dashboard_owned_test"))
        connection.execute(text("CREATE TABLE health_dashboard_owned_test (id integer)"))
        connection.execute(
            text("ALTER TABLE health_dashboard_owned_test OWNER TO health_dashboard")
        )
    try:
        with pytest.raises(DBAPIError, match="health_dashboard owns database objects"):
            ensure_dashboard_reader(settings, engine=engine)
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE health_dashboard_owned_test OWNER TO health_agent")
            )
            connection.execute(text("DROP TABLE health_dashboard_owned_test"))
        ensure_dashboard_reader(settings, engine=engine)


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
            admin_email="health-agent@localhost.local",
        ),
    )

    result = CliRunner().invoke(cli.app, ["dashboard", "setup"])

    assert result.exit_code == 0
    assert result.stdout == (
        "status=ready dashboard_id=3 card_id=4 "
        "url=http://127.0.0.1:53000/dashboard/3 "
        "admin_email=health-agent@localhost.local\n"
    )
