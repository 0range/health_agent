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
        login_email = _valid_metabase_email(email)
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
                        "email": login_email,
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
                json={"username": login_email, "password": password},
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
        client.authenticate(settings.metabase_admin_email, settings.postgres_password)
        collection = _ensure_named(
            client,
            path="/api/collection",
            name=COLLECTION_NAME,
            payload={"name": COLLECTION_NAME},
        )
        database = _ensure_named(
            client,
            path="/api/database",
            name=DATABASE_NAME,
            payload=_database_payload(settings),
        )
        dashboard = _ensure_named(
            client,
            path="/api/dashboard",
            name=DASHBOARD_NAME,
            payload={"name": DASHBOARD_NAME, "collection_id": collection["id"]},
        )
        card = _ensure_named(
            client,
            path="/api/card",
            name=CARD_NAME,
            payload=_card_payload(database["id"], collection["id"]),
        )
        _ensure_dashboard_card(client, dashboard["id"], card["id"])

    dashboard_url = f"{settings.metabase_url.rstrip('/')}/dashboard/{dashboard['id']}"
    return MetabaseBootstrapResult(
        collection_id=collection["id"],
        database_id=database["id"],
        dashboard_id=dashboard["id"],
        card_id=card["id"],
        dashboard_url=dashboard_url,
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
            text("SELECT set_config('health_agent.dashboard_password', :password, true)"),
            {"password": settings.postgres_password},
        )
        connection.execute(
            text(
                "DO $$ BEGIN EXECUTE format('ALTER ROLE health_dashboard WITH LOGIN "
                "PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION "
                "NOBYPASSRLS', current_setting('health_agent.dashboard_password')); END $$"
            )
        )
        connection.execute(
            text(f"REVOKE ALL PRIVILEGES ON DATABASE {database_name} FROM health_dashboard")
        )
        connection.execute(
            text(f"GRANT CONNECT ON DATABASE {database_name} TO health_dashboard")
        )
        connection.execute(text("REVOKE ALL PRIVILEGES ON SCHEMA public FROM health_dashboard"))
        connection.execute(text("GRANT USAGE ON SCHEMA public TO health_dashboard"))
        connection.execute(
            text(
                "REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public "
                "FROM health_dashboard"
            )
        )
        connection.execute(
            text("GRANT SELECT ON ALL TABLES IN SCHEMA public TO health_dashboard")
        )
        connection.execute(
            text(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner_name} IN SCHEMA public "
                "GRANT SELECT ON TABLES TO health_dashboard"
            )
        )


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _valid_metabase_email(email: str) -> str:
    local, separator, domain = email.rpartition("@")
    if separator and local and "." not in domain:
        return f"{local}@{domain}.local"
    return email


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


def _ensure_named(
    client: MetabaseClient,
    *,
    path: str,
    name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    response = client.request("GET", path)
    rows = response.get("data", []) if isinstance(response, dict) else response
    if not isinstance(rows, list):
        raise TypeError(f"Unexpected Metabase response from {path}")
    for row in rows:
        if isinstance(row, dict) and row.get("name") == name and not row.get("archived"):
            return row
    created = client.request("POST", path, json=payload)
    if not isinstance(created, dict) or not isinstance(created.get("id"), int):
        raise TypeError(f"Metabase did not create {name}")
    return created


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
