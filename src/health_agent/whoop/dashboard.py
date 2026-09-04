from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import Engine

from health_agent.config import Settings
from health_agent.metabase import (
    MetabaseClient,
    _candidate,
    _ensure_collection,
    _ensure_dashboard_card,
    _ensure_database,
    _native_query,
    _require_entity,
    _rows,
    ensure_dashboard_reader,
)
from health_agent.models import DEFAULT_PROFILE_ID

WHOOP_DASHBOARD_NAME = "WHOOP — сон и восстановление"


@dataclass(frozen=True)
class WhoopCardSpec:
    name: str
    query: str
    metrics: tuple[str, ...]


@dataclass(frozen=True)
class WhoopDashboardResult:
    dashboard_id: int
    card_ids: tuple[int, ...]
    dashboard_url: str


def whoop_card_specs(profile_id: UUID) -> tuple[WhoopCardSpec, ...]:
    """Return profile-bound queries; UUID typing prevents SQL injection."""
    profile = str(profile_id)
    daily_filter = f"profile_id = '{profile}' AND day IS NOT NULL"
    return (
        WhoopCardSpec(
            "WHOOP — Recovery и strain",
            "SELECT day AS date, recovery_score, strain FROM whoop_daily_health "
            f"WHERE {daily_filter} AND (recovery_score IS NOT NULL OR strain IS NOT NULL) "
            "ORDER BY date",
            ("recovery_score", "strain"),
        ),
        WhoopCardSpec(
            "WHOOP — HRV и пульс покоя",
            "SELECT day AS date, hrv_rmssd_milli, resting_heart_rate "
            f"FROM whoop_daily_health WHERE {daily_filter} "
            "AND (hrv_rmssd_milli IS NOT NULL OR resting_heart_rate IS NOT NULL) "
            "ORDER BY date",
            ("hrv_rmssd_milli", "resting_heart_rate"),
        ),
        WhoopCardSpec(
            "WHOOP — длительность сна",
            "SELECT day AS date, total_sleep_milli / 3600000.0 AS sleep_hours "
            "FROM whoop_sleep_history "
            f"WHERE {daily_filter} AND is_nap = false AND total_sleep_milli IS NOT NULL "
            "ORDER BY date",
            ("sleep_hours",),
        ),
        WhoopCardSpec(
            "WHOOP — качество сна",
            "SELECT day AS date, sleep_performance_percentage, "
            "sleep_efficiency_percentage FROM whoop_sleep_history "
            f"WHERE {daily_filter} AND is_nap = false "
            "AND (sleep_performance_percentage IS NOT NULL "
            "OR sleep_efficiency_percentage IS NOT NULL) ORDER BY date",
            ("sleep_performance_percentage", "sleep_efficiency_percentage"),
        ),
        WhoopCardSpec(
            "WHOOP — вес",
            "SELECT observed_at::date AS date, weight_kilogram "
            "FROM whoop_body_snapshot "
            f"WHERE profile_id = '{profile}' AND observed_at IS NOT NULL "
            "AND weight_kilogram IS NOT NULL ORDER BY observed_at",
            ("weight_kilogram",),
        ),
    )


def bootstrap_whoop_dashboard(
    settings: Settings,
    profile_id: UUID,
    *,
    transport: httpx.BaseTransport | None = None,
    engine: Engine | None = None,
) -> WhoopDashboardResult:
    """Idempotently provision one profile-isolated WHOOP dashboard."""
    ensure_dashboard_reader(settings, engine=engine)
    suffix = "" if profile_id == DEFAULT_PROFILE_ID else f" [{profile_id}]"
    legacy_suffix = (
        f" [{profile_id}]"
        if profile_id == DEFAULT_PROFILE_ID
        else f" [{str(profile_id)[:8]}]"
    )
    dashboard_name = f"{WHOOP_DASHBOARD_NAME}{suffix}"
    specs = whoop_card_specs(profile_id)
    with MetabaseClient(settings.metabase_url, transport=transport) as client:
        client.wait_until_healthy()
        client.authenticate(
            settings.effective_metabase_admin_email, settings.postgres_password
        )
        collection = _ensure_collection(client)
        database = _ensure_database(client, settings)
        legacy_reusable = profile_id == DEFAULT_PROFILE_ID or _legacy_objects_owned_by(
            client,
            collection["id"],
            specs,
            legacy_suffix,
        )
        dashboard = _ensure_named_dashboard(
            client,
            collection["id"],
            dashboard_name,
            legacy_name=(
                f"{WHOOP_DASHBOARD_NAME}{legacy_suffix}"
                if legacy_reusable
                else None
            ),
        )
        card_ids: list[int] = []
        for position, spec in enumerate(specs):
            card = _ensure_whoop_card(
                client,
                database["id"],
                collection["id"],
                spec,
                suffix,
                legacy_suffix=legacy_suffix if legacy_reusable else None,
            )
            card_ids.append(card["id"])
            _ensure_dashboard_card(
                client,
                dashboard["id"],
                card["id"],
                row=(position // 2) * 8,
                col=(position % 2) * 12,
                size_x=12,
                size_y=8,
            )
    return WhoopDashboardResult(
        dashboard_id=dashboard["id"],
        card_ids=tuple(card_ids),
        dashboard_url=f"{settings.metabase_url}/dashboard/{dashboard['id']}",
    )


def _ensure_named_dashboard(
    client: MetabaseClient,
    collection_id: int,
    name: str,
    *,
    legacy_name: str | None = None,
) -> dict[str, Any]:
    desired = {"name": name, "collection_id": collection_id}
    existing = _candidate(
        _rows(client, "/api/dashboard"),
        name,
        expected_parent=("collection_id", collection_id),
    )
    if existing is None and legacy_name is not None:
        existing = _candidate(
            _rows(client, "/api/dashboard"),
            legacy_name,
            expected_parent=("collection_id", collection_id),
        )
    method = "POST" if existing is None else "PUT"
    path = "/api/dashboard" if existing is None else f"/api/dashboard/{existing['id']}"
    dashboard = _require_entity(client.request(method, path, json=desired), name)
    if dashboard.get("name") != name or dashboard.get("collection_id") != collection_id:
        raise RuntimeError("Metabase WHOOP dashboard reconciliation failed")
    return dashboard


def _ensure_whoop_card(
    client: MetabaseClient,
    database_id: int,
    collection_id: int,
    spec: WhoopCardSpec,
    profile_suffix: str,
    *,
    legacy_suffix: str | None = None,
) -> dict[str, Any]:
    managed_name = f"{spec.name}{profile_suffix}"
    desired = {
        "name": managed_name,
        "collection_id": collection_id,
        "display": "line",
        "dataset_query": {
            "database": database_id,
            "type": "native",
            "native": {"query": spec.query, "template-tags": {}},
        },
        "visualization_settings": {
            "graph.dimensions": ["date"],
            "graph.metrics": list(spec.metrics),
        },
    }
    existing = _candidate(
        _rows(client, "/api/card"),
        managed_name,
        expected_parent=("collection_id", collection_id),
    )
    if existing is None and legacy_suffix is not None:
        existing = _candidate(
            _rows(client, "/api/card"),
            f"{spec.name}{legacy_suffix}",
            expected_parent=("collection_id", collection_id),
        )
    method = "POST" if existing is None else "PUT"
    path = "/api/card" if existing is None else f"/api/card/{existing['id']}"
    card = _require_entity(client.request(method, path, json=desired), managed_name)
    if not _whoop_card_matches(card, desired):
        raise RuntimeError(f"Metabase WHOOP card reconciliation failed: {managed_name}")
    return card


def _whoop_card_matches(card: dict[str, Any], desired: dict[str, Any]) -> bool:
    database_id, query = _native_query(card.get("dataset_query"))
    desired_database_id, desired_query = _native_query(desired["dataset_query"])
    return (
        card.get("name") == desired["name"]
        and card.get("collection_id") == desired["collection_id"]
        and card.get("display") == "line"
        and database_id == desired_database_id
        and query == desired_query
        and card.get("visualization_settings") == desired["visualization_settings"]
    )


def _legacy_objects_owned_by(
    client: MetabaseClient,
    collection_id: int,
    specs: tuple[WhoopCardSpec, ...],
    legacy_suffix: str,
) -> bool:
    """Reuse an ambiguous short-name legacy set only after exact SQL ownership proof."""
    cards = _rows(client, "/api/card")
    owned_card_ids: set[int] = set()
    for spec in specs:
        candidates = [
            card
            for card in cards
            if card.get("name") == f"{spec.name}{legacy_suffix}"
            and card.get("collection_id") == collection_id
            and not card.get("archived")
        ]
        if len(candidates) != 1:
            return False
        card = candidates[0]
        _, query = _native_query(card.get("dataset_query"))
        card_id = card.get("id")
        if (
            not isinstance(card_id, int)
            or card.get("display") != "line"
            or query != spec.query
            or card.get("visualization_settings")
            != {
                "graph.dimensions": ["date"],
                "graph.metrics": list(spec.metrics),
            }
        ):
            return False
        owned_card_ids.add(card_id)

    legacy_dashboard = _candidate(
        _rows(client, "/api/dashboard"),
        f"{WHOOP_DASHBOARD_NAME}{legacy_suffix}",
        expected_parent=("collection_id", collection_id),
    )
    if legacy_dashboard is None:
        return False
    details = client.request("GET", f"/api/dashboard/{legacy_dashboard['id']}")
    if not isinstance(details, dict) or not isinstance(details.get("dashcards"), list):
        return False
    attached_ids = {
        item.get("card_id")
        for item in details["dashcards"]
        if isinstance(item, dict) and isinstance(item.get("card_id"), int)
    }
    return attached_ids == owned_card_ids
