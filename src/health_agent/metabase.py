from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Self

import httpx
from sqlalchemy import Engine, text

from health_agent.config import Settings
from health_agent.db import build_engine

COLLECTION_NAME = "Health Agent"
DATABASE_NAME = "Health Agent"
DASHBOARD_NAME = "Анализы крови"
CARD_NAME = "Динамика анализов крови"
READER_ROLE = "health_dashboard"


@dataclass(frozen=True)
class MetabaseBootstrapResult:
    collection_id: int
    database_id: int
    dashboard_id: int
    card_id: int
    dashboard_url: str
    admin_email: str


class MetabaseClient:
    """Narrow client for the local Metabase bootstrap API."""

    def __init__(
        self,
        base_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=10,
            transport=transport,
        )
        self._session_id: str | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self._client.close()

    def wait_until_healthy(self, attempts: int = 60, delay_seconds: float = 1) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                response = self._client.get("/api/health")
                response.raise_for_status()
                if response.json().get("status") == "ok":
                    return
            except (httpx.HTTPError, ValueError) as error:
                last_error = error
            if attempt + 1 < attempts:
                time.sleep(delay_seconds)
        raise RuntimeError("Metabase did not become healthy") from last_error

    def authenticate(self, email: str, password: str) -> None:
        properties = self.request("GET", "/api/session/properties", authenticated=False)
        assert isinstance(properties, dict)
        setup_token = properties.get("setup-token")
        if setup_token:
            response = self.request(
                "POST",
                "/api/setup",
                authenticated=False,
                json={
                    "token": setup_token,
                    "user": {
                        "email": email,
                        "password": password,
                        "first_name": "Health",
                        "last_name": "Agent",
                    },
                    "prefs": {"site_name": COLLECTION_NAME},
                },
            )
        else:
            response = self.request(
                "POST",
                "/api/session",
                authenticated=False,
                json={"username": email, "password": password},
            )
        assert isinstance(response, dict)
        session_id = response.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("Metabase authentication returned no session id")
        self._session_id = session_id

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        json: dict[str, Any] | None = None,
    ) -> Any:
        headers = {}
        if authenticated:
            if self._session_id is None:
                raise RuntimeError("Metabase client is not authenticated")
            headers["X-Metabase-Session"] = self._session_id
        response = self._client.request(method, path, headers=headers, json=json)
        response.raise_for_status()
        return response.json()


def bootstrap_metabase(
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
    engine: Engine | None = None,
) -> MetabaseBootstrapResult:
    """Idempotently provision the read-only laboratory dashboard."""
    ensure_dashboard_reader(settings, engine=engine)

    with MetabaseClient(settings.metabase_url, transport=transport) as client:
        client.wait_until_healthy()
        client.authenticate(
            settings.effective_metabase_admin_email, settings.postgres_password
        )
        collection = _ensure_collection(client)
        database = _ensure_database(client, settings)
        dashboard = _ensure_dashboard(client, collection["id"])
        card = _ensure_card(client, database["id"], collection["id"])
        _ensure_dashboard_card(client, dashboard["id"], card["id"])

    dashboard_url = f"{settings.metabase_url.rstrip('/')}/dashboard/{dashboard['id']}"
    return MetabaseBootstrapResult(
        collection_id=collection["id"],
        database_id=database["id"],
        dashboard_id=dashboard["id"],
        card_id=card["id"],
        dashboard_url=dashboard_url,
        admin_email=settings.effective_metabase_admin_email,
    )


def ensure_dashboard_reader(settings: Settings, *, engine: Engine | None = None) -> None:
    """Create and constrain the PostgreSQL login used by Metabase."""
    database_engine = engine or build_engine(settings)
    database_name = _quote_identifier(settings.postgres_database)
    owner_name = _quote_identifier(settings.postgres_user)
    with database_engine.begin() as connection:
        connection.execute(
            text(
                "DO $$ BEGIN CREATE ROLE health_dashboard; "
                "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
            )
        )
        connection.execute(
            text(
                "DO $$ BEGIN IF EXISTS ("
                "SELECT 1 FROM pg_database database_object "
                "JOIN pg_roles owner ON owner.oid = database_object.datdba "
                "WHERE owner.rolname = 'health_dashboard' UNION ALL "
                "SELECT 1 FROM pg_namespace namespace_object "
                "JOIN pg_roles owner ON owner.oid = namespace_object.nspowner "
                "WHERE owner.rolname = 'health_dashboard' UNION ALL "
                "SELECT 1 FROM pg_class relation_object "
                "JOIN pg_roles owner ON owner.oid = relation_object.relowner "
                "WHERE owner.rolname = 'health_dashboard' UNION ALL "
                "SELECT 1 FROM pg_proc routine_object "
                "JOIN pg_roles owner ON owner.oid = routine_object.proowner "
                "WHERE owner.rolname = 'health_dashboard' UNION ALL "
                "SELECT 1 FROM pg_type type_object "
                "JOIN pg_roles owner ON owner.oid = type_object.typowner "
                "WHERE owner.rolname = 'health_dashboard'"
                ") THEN RAISE EXCEPTION 'health_dashboard owns database objects'; "
                "END IF; END $$"
            )
        )
        connection.execute(
            text(
                "DO $$ DECLARE parent_role record; BEGIN "
                "FOR parent_role IN SELECT parent.rolname FROM pg_auth_members membership "
                "JOIN pg_roles child ON child.oid = membership.member "
                "JOIN pg_roles parent ON parent.oid = membership.roleid "
                "WHERE child.rolname = 'health_dashboard' LOOP "
                "EXECUTE format('REVOKE %I FROM health_dashboard', parent_role.rolname); "
                "END LOOP; END $$"
            )
        )
        connection.execute(
            text("SELECT set_config('health_agent.dashboard_password', :password, true)"),
            {"password": settings.postgres_password},
        )
        connection.execute(
            text(
                "DO $$ BEGIN EXECUTE format('ALTER ROLE health_dashboard WITH LOGIN "
                "PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS NOINHERIT', "
                "current_setting('health_agent.dashboard_password')); END $$"
            )
        )
        connection.execute(
            text(
                "DO $$ DECLARE database_object record; BEGIN FOR database_object IN "
                "SELECT datname FROM pg_database LOOP "
                "EXECUTE format('REVOKE ALL PRIVILEGES ON DATABASE %I "
                "FROM health_dashboard', database_object.datname); END LOOP; END $$"
            )
        )
        connection.execute(
            text(f"GRANT CONNECT ON DATABASE {database_name} TO health_dashboard")
        )
        connection.execute(
            text(
                "DO $$ DECLARE object_schema record; BEGIN FOR object_schema IN "
                "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
                "AND nspname <> 'information_schema' LOOP "
                "EXECUTE format('REVOKE ALL PRIVILEGES ON SCHEMA %I FROM health_dashboard', "
                "object_schema.nspname); "
                "EXECUTE format('REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA %I "
                "FROM health_dashboard', object_schema.nspname); "
                "EXECUTE format('REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA %I "
                "FROM health_dashboard', object_schema.nspname); "
                "EXECUTE format('REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA %I "
                "FROM health_dashboard', object_schema.nspname); END LOOP; END $$"
            )
        )
        connection.execute(
            text(
                "DO $$ DECLARE default_grant record; object_kind text; command text; BEGIN "
                "FOR default_grant IN SELECT DISTINCT owner.rolname AS owner_name, "
                "namespace.nspname AS schema_name, defaults.defaclobjtype AS object_type "
                "FROM pg_default_acl defaults "
                "JOIN pg_roles owner ON owner.oid = defaults.defaclrole "
                "LEFT JOIN pg_namespace namespace ON namespace.oid = defaults.defaclnamespace "
                "CROSS JOIN LATERAL aclexplode(defaults.defaclacl) privilege "
                "JOIN pg_roles grantee ON grantee.oid = privilege.grantee "
                "WHERE grantee.rolname = 'health_dashboard' LOOP "
                "object_kind := CASE default_grant.object_type "
                "WHEN 'r' THEN 'TABLES' WHEN 'S' THEN 'SEQUENCES' "
                "WHEN 'f' THEN 'FUNCTIONS' WHEN 'T' THEN 'TYPES' "
                "WHEN 'n' THEN 'SCHEMAS' END; "
                "IF object_kind IS NULL THEN RAISE EXCEPTION "
                "'unsupported health_dashboard default privilege type'; END IF; "
                "IF default_grant.schema_name IS NULL OR object_kind = 'SCHEMAS' THEN "
                "command := format('ALTER DEFAULT PRIVILEGES FOR ROLE %I REVOKE ALL "
                "PRIVILEGES ON %s FROM health_dashboard', default_grant.owner_name, "
                "object_kind); ELSE command := format('ALTER DEFAULT PRIVILEGES FOR ROLE "
                "%I IN SCHEMA %I REVOKE ALL PRIVILEGES ON %s FROM health_dashboard', "
                "default_grant.owner_name, default_grant.schema_name, object_kind); END IF; "
                "EXECUTE command; END LOOP; END $$"
            )
        )
        connection.execute(text("GRANT USAGE ON SCHEMA public TO health_dashboard"))
        connection.execute(
            text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO health_dashboard")
        )
        connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_name} IN SCHEMA public "
                "GRANT SELECT ON TABLES TO health_dashboard"
            )
        )
        connection.execute(
            text(
                "DO $$ BEGIN "
                "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'health_dashboard' "
                "AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication "
                "OR rolbypassrls OR rolinherit OR NOT rolcanlogin)) THEN "
                "RAISE EXCEPTION 'health_dashboard has unsafe role attributes'; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_auth_members membership "
                "JOIN pg_roles child ON child.oid = membership.member "
                "WHERE child.rolname = 'health_dashboard') THEN "
                "RAISE EXCEPTION 'health_dashboard retains role memberships'; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_class relation_object "
                "JOIN pg_namespace namespace ON namespace.oid = relation_object.relnamespace "
                "WHERE namespace.nspname !~ '^pg_' AND namespace.nspname <> 'information_schema' "
                "AND (has_table_privilege('health_dashboard', relation_object.oid, 'INSERT') "
                "OR has_table_privilege('health_dashboard', relation_object.oid, 'UPDATE') "
                "OR has_table_privilege('health_dashboard', relation_object.oid, 'DELETE') "
                "OR has_table_privilege('health_dashboard', relation_object.oid, 'TRUNCATE') "
                "OR has_table_privilege('health_dashboard', relation_object.oid, 'REFERENCES') "
                "OR has_table_privilege('health_dashboard', relation_object.oid, 'TRIGGER'))) "
                "THEN RAISE EXCEPTION 'health_dashboard retains table write privileges'; END IF; "
                "IF EXISTS (SELECT 1 FROM pg_namespace namespace "
                "WHERE namespace.nspname !~ '^pg_' AND namespace.nspname <> 'information_schema' "
                "AND has_schema_privilege('health_dashboard', namespace.oid, 'CREATE')) "
                "THEN RAISE EXCEPTION 'health_dashboard retains schema create privileges'; END IF; "
                "IF has_database_privilege('health_dashboard', current_database(), 'CREATE') "
                "THEN RAISE EXCEPTION 'health_dashboard retains database create privileges'; END IF; "
                "END $$"
            )
        )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_payload(settings: Settings) -> dict[str, Any]:
    return {
        "name": DATABASE_NAME,
        "engine": "postgres",
        "details": {
            "host": "postgres",
            "port": 5432,
            "dbname": settings.postgres_database,
            "user": READER_ROLE,
            "password": settings.postgres_password,
            "ssl": False,
        },
        "is_full_sync": True,
    }


def _card_payload(database_id: int, collection_id: int) -> dict[str, Any]:
    query = (
        "SELECT created_at::date AS date, normalized_value, canonical_name "
        "FROM verified_lab_history "
        "WHERE normalized_value IS NOT NULL "
        "ORDER BY date, canonical_name"
    )
    return {
        "name": CARD_NAME,
        "collection_id": collection_id,
        "display": "line",
        "dataset_query": {
            "database": database_id,
            "type": "native",
            "native": {"query": query, "template-tags": {}},
        },
        "visualization_settings": {
            "graph.dimensions": ["date", "canonical_name"],
            "graph.metrics": ["normalized_value"],
        },
    }


def _rows(client: MetabaseClient, path: str) -> list[dict[str, Any]]:
    response = client.request("GET", path)
    rows = response.get("data", []) if isinstance(response, dict) else response
    if not isinstance(rows, list):
        raise TypeError(f"Unexpected Metabase response from {path}")
    return [row for row in rows if isinstance(row, dict)]


def _candidate(
    rows: list[dict[str, Any]],
    name: str,
    *,
    expected_parent: tuple[str, int | None] | None = None,
) -> dict[str, Any] | None:
    candidates = [
        row for row in rows if row.get("name") == name and not row.get("archived")
    ]
    if expected_parent is not None:
        field, expected_id = expected_parent
        exact = [row for row in candidates if row.get(field) == expected_id]
        if len(exact) == 1:
            return exact[0]
        if len(exact) > 1:
            raise RuntimeError(f"Multiple managed Metabase objects named {name}")
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise RuntimeError(f"Ambiguous Metabase name collision for {name}")
    return None


def _require_entity(response: Any, name: str) -> dict[str, Any]:
    if not isinstance(response, dict) or not isinstance(response.get("id"), int):
        raise TypeError(f"Metabase did not reconcile {name}")
    return response


def _ensure_collection(client: MetabaseClient) -> dict[str, Any]:
    desired = {"name": COLLECTION_NAME, "parent_id": None}
    existing = _candidate(
        _rows(client, "/api/collection"),
        COLLECTION_NAME,
        expected_parent=("parent_id", None),
    )
    if existing is None:
        collection = _require_entity(
            client.request("POST", "/api/collection", json=desired), COLLECTION_NAME
        )
    else:
        collection = _require_entity(
            client.request("PUT", f"/api/collection/{existing['id']}", json=desired),
            COLLECTION_NAME,
        )
    if collection.get("name") != COLLECTION_NAME or collection.get("parent_id") is not None:
        raise RuntimeError("Metabase collection reconciliation failed")
    return collection


def _database_matches(database: dict[str, Any], desired: dict[str, Any]) -> bool:
    details = database.get("details")
    expected_details = desired["details"]
    if not isinstance(details, dict) or not isinstance(expected_details, dict):
        return False
    return database.get("engine") == "postgres" and all(
        details.get(key) == expected_details[key]
        for key in ("host", "port", "dbname", "user", "ssl")
    )


def _ensure_database(client: MetabaseClient, settings: Settings) -> dict[str, Any]:
    desired = _database_payload(settings)
    candidates = [
        row
        for row in _rows(client, "/api/database")
        if row.get("name") == DATABASE_NAME and not row.get("archived")
    ]
    matching = [row for row in candidates if _database_matches(row, desired)]
    if len(matching) == 1:
        existing = matching[0]
    elif len(candidates) == 1:
        existing = candidates[0]
    elif candidates:
        raise RuntimeError(f"Ambiguous Metabase name collision for {DATABASE_NAME}")
    else:
        existing = None
    if existing is None:
        database = _require_entity(
            client.request("POST", "/api/database", json=desired), DATABASE_NAME
        )
    else:
        database = _require_entity(
            client.request("PUT", f"/api/database/{existing['id']}", json=desired),
            DATABASE_NAME,
        )
    if database.get("name") != DATABASE_NAME or not _database_matches(database, desired):
        raise RuntimeError("Metabase database reconciliation failed")
    return database


def _ensure_dashboard(client: MetabaseClient, collection_id: int) -> dict[str, Any]:
    desired = {"name": DASHBOARD_NAME, "collection_id": collection_id}
    existing = _candidate(
        _rows(client, "/api/dashboard"),
        DASHBOARD_NAME,
        expected_parent=("collection_id", collection_id),
    )
    if existing is None:
        dashboard = _require_entity(
            client.request("POST", "/api/dashboard", json=desired), DASHBOARD_NAME
        )
    else:
        dashboard = _require_entity(
            client.request("PUT", f"/api/dashboard/{existing['id']}", json=desired),
            DASHBOARD_NAME,
        )
    if (
        dashboard.get("name") != DASHBOARD_NAME
        or dashboard.get("collection_id") != collection_id
    ):
        raise RuntimeError("Metabase dashboard reconciliation failed")
    return dashboard


def _native_query(dataset_query: Any) -> tuple[int | None, str | None]:
    if not isinstance(dataset_query, dict):
        return None, None
    database_id = dataset_query.get("database")
    native = dataset_query.get("native")
    if isinstance(native, dict):
        query = native.get("query")
        return database_id, query if isinstance(query, str) else None
    stages = dataset_query.get("stages")
    if isinstance(stages, list) and stages and isinstance(stages[0], dict):
        query = stages[0].get("native")
        return database_id, query if isinstance(query, str) else None
    return database_id, None


def _card_matches(card: dict[str, Any], desired: dict[str, Any]) -> bool:
    database_id, query = _native_query(card.get("dataset_query"))
    desired_database_id, desired_query = _native_query(desired["dataset_query"])
    visualization = card.get("visualization_settings")
    desired_visualization = desired["visualization_settings"]
    if not isinstance(visualization, dict) or not isinstance(desired_visualization, dict):
        return False
    return (
        card.get("name") == CARD_NAME
        and card.get("collection_id") == desired["collection_id"]
        and card.get("display") == "line"
        and database_id == desired_database_id
        and query == desired_query
        and visualization.get("graph.dimensions")
        == desired_visualization["graph.dimensions"]
        and visualization.get("graph.metrics") == desired_visualization["graph.metrics"]
    )


def _ensure_card(
    client: MetabaseClient, database_id: int, collection_id: int
) -> dict[str, Any]:
    desired = _card_payload(database_id, collection_id)
    existing = _candidate(
        _rows(client, "/api/card"),
        CARD_NAME,
        expected_parent=("collection_id", collection_id),
    )
    if existing is None:
        card = _require_entity(
            client.request("POST", "/api/card", json=desired), CARD_NAME
        )
    else:
        card = _require_entity(
            client.request("PUT", f"/api/card/{existing['id']}", json=desired),
            CARD_NAME,
        )
    if not _card_matches(card, desired):
        raise RuntimeError("Metabase card reconciliation failed")
    return card


def _ensure_dashboard_card(
    client: MetabaseClient, dashboard_id: int, card_id: int
) -> None:
    dashboard = client.request("GET", f"/api/dashboard/{dashboard_id}")
    if not isinstance(dashboard, dict):
        raise TypeError("Unexpected Metabase dashboard response")
    dashcards = dashboard.get("dashcards", [])
    if not isinstance(dashcards, list):
        raise TypeError("Unexpected Metabase dashboard cards response")
    if any(row.get("card_id") == card_id for row in dashcards if isinstance(row, dict)):
        return
    client.request(
        "PUT",
        f"/api/dashboard/{dashboard_id}",
        json={
            "dashcards": [
                *dashcards,
                {
                    "id": -1,
                    "card_id": card_id,
                    "row": 0,
                    "col": 0,
                    "size_x": 24,
                    "size_y": 8,
                    "parameter_mappings": [],
                    "visualization_settings": {},
                },
            ]
        },
    )
