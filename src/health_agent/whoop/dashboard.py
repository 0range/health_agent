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
    display: str = "line"
    description: str = ""
    unit: str = ""
    x_axis_title: str = "Дата"
    y_axis_title: str = ""
    legacy_name: str | None = None


@dataclass(frozen=True)
class WhoopDashboardResult:
    dashboard_id: int
    card_ids: tuple[int, ...]
    dashboard_url: str


def whoop_card_specs(profile_id: UUID) -> tuple[WhoopCardSpec, ...]:
    """Return profile-bound queries; UUID typing prevents SQL injection."""
    if not isinstance(profile_id, UUID):
        raise TypeError("profile_id must be a UUID")
    profile = str(profile_id)
    return (
        WhoopCardSpec(
            "WHOOP — Recovery",
            _recovery_query(
                profile, "recovery_score", "r.recovery_score BETWEEN 0 AND 100"
            ),
            ("recovery_score",),
            description=(
                "Recovery WHOOP за день: последняя валидная запись, не среднее. "
                "Сегодняшнее значение может обновиться. Это наблюдение, не диагноз "
                "и не доказательство причин или следствий."
            ),
            unit="%",
            y_axis_title="Recovery, %",
            legacy_name="WHOOP — Recovery и strain",
        ),
        WhoopCardSpec(
            "WHOOP — strain",
            _cycle_query(profile, "strain", "c.strain BETWEEN 0 AND 21"),
            ("strain",),
            description=(
                "Strain WHOOP за день по шкале 0–21: последняя валидная запись, "
                "не среднее. Сегодняшнее значение может обновиться. Это наблюдение, "
                "не диагноз и не доказательство причин или следствий."
            ),
            unit="0–21",
            y_axis_title="Strain WHOOP, 0–21",
        ),
        WhoopCardSpec(
            "WHOOP — HRV",
            _recovery_query(
                profile,
                "hrv_rmssd_milli",
                "r.hrv_rmssd_milli > 0 "
                "AND r.hrv_rmssd_milli::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            ),
            ("hrv_rmssd_milli",),
            description=(
                "HRV (RMSSD) за день в миллисекундах: последняя валидная запись, "
                "не среднее. Сегодняшнее значение может обновиться. Это наблюдение, "
                "не диагноз и не доказательство причин или следствий."
            ),
            unit="мс",
            y_axis_title="HRV (RMSSD), мс",
            legacy_name="WHOOP — HRV и пульс покоя",
        ),
        WhoopCardSpec(
            "WHOOP — пульс покоя",
            _recovery_query(
                profile,
                "resting_heart_rate",
                "r.resting_heart_rate > 0 "
                "AND r.resting_heart_rate::text NOT IN ('NaN', 'Infinity', '-Infinity')",
            ),
            ("resting_heart_rate",),
            description=(
                "Пульс покоя WHOOP за день: последняя валидная запись, не среднее. "
                "Сегодняшнее значение может обновиться. Это наблюдение, не диагноз "
                "и не доказательство причин или следствий."
            ),
            unit="уд/мин",
            y_axis_title="Пульс покоя, уд/мин",
        ),
        WhoopCardSpec(
            "WHOOP — длительность сна",
            _sleep_query(
                profile,
                "s.total_sleep_milli / 3600000.0",
                "sleep_hours",
                "s.total_sleep_milli > 0 AND s.total_sleep_milli <= 86400000",
            ),
            ("sleep_hours",),
            description=(
                "Длительность основного сна за день в часах: последняя валидная "
                "запись, не среднее. Сегодняшнее значение может обновиться. Это "
                "наблюдение, не диагноз и не доказательство причин или следствий."
            ),
            unit="ч",
            y_axis_title="Длительность сна, ч",
            legacy_name="WHOOP — длительность сна",
        ),
        WhoopCardSpec(
            "WHOOP — выполнение потребности во сне",
            _sleep_query(
                profile,
                "s.sleep_performance_percentage",
                "sleep_performance_percentage",
                "s.sleep_performance_percentage BETWEEN 0 AND 100",
            ),
            ("sleep_performance_percentage",),
            description=(
                "Sleep performance показывает процент выполнения рассчитанной WHOOP "
                "потребности во сне, а не общую оценку качества сна. Показана последняя "
                "валидная запись дня, не среднее; сегодняшний день может обновиться. "
                "Это наблюдение, не диагноз и не доказательство причин или следствий."
            ),
            unit="%",
            y_axis_title="Выполнение потребности во сне, %",
            legacy_name="WHOOP — качество сна",
        ),
        WhoopCardSpec(
            "WHOOP — эффективность сна",
            _sleep_query(
                profile,
                "s.sleep_efficiency_percentage",
                "sleep_efficiency_percentage",
                "s.sleep_efficiency_percentage BETWEEN 0 AND 100",
            ),
            ("sleep_efficiency_percentage",),
            description=(
                "Эффективность основного сна WHOOP за день: последняя валидная запись, "
                "не среднее. Сегодняшнее значение может обновиться. Это наблюдение, "
                "не диагноз и не доказательство причин или следствий."
            ),
            unit="%",
            y_axis_title="Эффективность сна, %",
        ),
        WhoopCardSpec(
            "WHOOP — вес",
            "SELECT weight_kilogram, observed_at FROM whoop_body_snapshot "
            f"WHERE profile_id = '{profile}' AND observed_at IS NOT NULL "
            "AND weight_kilogram > 0 "
            "AND weight_kilogram::text NOT IN ('NaN', 'Infinity', '-Infinity') "
            "ORDER BY observed_at DESC, connection_id DESC LIMIT 1",
            ("weight_kilogram",),
            display="table",
            description=(
                "Последний валидный снимок веса WHOOP. observed_at — время получения "
                "данных, а не подтверждённое время взвешивания. Это наблюдение, не "
                "диагноз и не доказательство причин или следствий."
            ),
            unit="кг",
            x_axis_title="",
            y_axis_title="Вес, кг",
            legacy_name="WHOOP — вес",
        ),
    )


def _cycle_query(profile: str, metric: str, validity: str) -> str:
    return (
        f"SELECT date, {metric} FROM ("
        f"SELECT DISTINCT ON (c.local_day) c.local_day AS date, c.{metric} AS {metric} "
        "FROM whoop_cycles c "
        f"WHERE c.profile_id = '{profile}' AND c.local_day IS NOT NULL "
        "AND c.local_day <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date "
        f"AND c.score_state = 'SCORED' AND {validity} "
        "ORDER BY c.local_day, c.start_at DESC, "
        "c.source_updated_at DESC NULLS LAST, c.id DESC"
        f") selected ORDER BY date"
    )


def _recovery_query(profile: str, metric: str, validity: str) -> str:
    return (
        f"SELECT date, {metric} FROM ("
        f"SELECT DISTINCT ON (c.local_day) c.local_day AS date, r.{metric} AS {metric} "
        "FROM whoop_cycles c JOIN whoop_recoveries r "
        "ON r.profile_id = c.profile_id AND r.connection_id = c.connection_id "
        "AND r.external_id = c.external_id "
        f"WHERE c.profile_id = '{profile}' AND c.local_day IS NOT NULL "
        "AND c.local_day <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date "
        f"AND r.score_state = 'SCORED' AND {validity} "
        "ORDER BY c.local_day, c.start_at DESC, "
        "r.source_updated_at DESC NULLS LAST, r.id DESC"
        f") selected ORDER BY date"
    )


def _sleep_query(profile: str, expression: str, metric: str, validity: str) -> str:
    return (
        f"SELECT date, {metric} FROM ("
        f"SELECT DISTINCT ON (s.local_day) s.local_day AS date, "
        f"{expression} AS {metric} FROM whoop_sleeps s "
        f"WHERE s.profile_id = '{profile}' AND s.local_day IS NOT NULL "
        "AND s.local_day <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date "
        f"AND s.score_state = 'SCORED' AND s.is_nap = false AND {validity} "
        "ORDER BY s.local_day, s.start_at DESC, "
        "s.source_updated_at DESC NULLS LAST, s.id DESC"
        f") selected ORDER BY date"
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
    legacy_specs = _legacy_whoop_card_specs(profile_id)
    with MetabaseClient(settings.metabase_url, transport=transport) as client:
        client.wait_until_healthy()
        client.authenticate(
            settings.effective_metabase_admin_email, settings.postgres_password
        )
        collection = _ensure_collection(client)
        database = _ensure_database(client, settings)
        short_old_shape = profile_id != DEFAULT_PROFILE_ID and (
            _objects_owned_by(
                client,
                collection["id"],
                legacy_specs,
                legacy_suffix,
                legacy=True,
            )
        )
        short_current_shape = profile_id != DEFAULT_PROFILE_ID and (
            _objects_owned_by(
                client,
                collection["id"],
                specs,
                legacy_suffix,
                legacy=False,
            )
        )
        short_name_reusable = short_old_shape or short_current_shape
        dashboard = _ensure_named_dashboard(
            client,
            collection["id"],
            dashboard_name,
            legacy_name=(
                f"{WHOOP_DASHBOARD_NAME}{legacy_suffix}"
                if short_name_reusable
                else None
            ),
        )
        card_ids: list[int] = []
        legacy_by_name = {spec.name: spec for spec in legacy_specs}
        for spec in specs:
            card = _ensure_whoop_card(
                client,
                database["id"],
                collection["id"],
                spec,
                suffix,
                legacy_spec=(
                    legacy_by_name.get(spec.legacy_name)
                    if spec.legacy_name is not None
                    else None
                ),
                short_legacy_suffix=(legacy_suffix if short_old_shape else None),
                short_current_suffix=(legacy_suffix if short_current_shape else None),
            )
            card_ids.append(card["id"])
        layouts = _managed_card_layouts(client, dashboard["id"], set(card_ids))
        for card_id, (row, col) in zip(card_ids, layouts, strict=True):
            _ensure_dashboard_card(
                client,
                dashboard["id"],
                card_id,
                row=row,
                col=col,
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
    legacy_spec: WhoopCardSpec | None = None,
    short_legacy_suffix: str | None = None,
    short_current_suffix: str | None = None,
) -> dict[str, Any]:
    managed_name = f"{spec.name}{profile_suffix}"
    desired = {
        "name": managed_name,
        "collection_id": collection_id,
        "display": spec.display,
        "description": spec.description,
        "dataset_query": {
            "database": database_id,
            "type": "native",
            "native": {"query": spec.query, "template-tags": {}},
        },
        "visualization_settings": _visualization_settings(spec),
    }
    existing = _candidate(
        _rows(client, "/api/card"),
        managed_name,
        expected_parent=("collection_id", collection_id),
    )
    if existing is None and short_current_suffix is not None:
        existing = _card_snapshot_candidate(
            client,
            collection_id,
            spec,
            short_current_suffix,
            legacy=False,
        )
    if existing is None and legacy_spec is not None:
        suffixes = [profile_suffix]
        if short_legacy_suffix is not None and short_legacy_suffix not in suffixes:
            suffixes.append(short_legacy_suffix)
        for legacy_candidate_suffix in suffixes:
            existing = _legacy_card_candidate(
                client,
                collection_id,
                legacy_spec,
                legacy_candidate_suffix,
            )
            if existing is not None:
                break
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
        and card.get("display") == desired["display"]
        and card.get("description") == desired["description"]
        and database_id == desired_database_id
        and query == desired_query
        and card.get("visualization_settings") == desired["visualization_settings"]
    )


def _objects_owned_by(
    client: MetabaseClient,
    collection_id: int,
    specs: tuple[WhoopCardSpec, ...],
    legacy_suffix: str,
    *,
    legacy: bool,
) -> bool:
    """Reuse an ambiguous short-name legacy set only after exact SQL ownership proof."""
    owned_card_ids: set[int] = set()
    for spec in specs:
        card = _card_snapshot_candidate(
            client,
            collection_id,
            spec,
            legacy_suffix,
            legacy=legacy,
        )
        if card is None:
            return False
        card_id = card.get("id")
        if not isinstance(card_id, int):
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
    return owned_card_ids <= attached_ids


def _visualization_settings(spec: WhoopCardSpec) -> dict[str, Any]:
    if spec.display == "table":
        return {
            "column_settings": {
                '["name","weight_kilogram"]': {"column_title": "Вес, кг"},
                '["name","observed_at"]': {"column_title": "Получено из WHOOP"},
            }
        }
    return {
        "graph.dimensions": ["date"],
        "graph.metrics": list(spec.metrics),
        "graph.x_axis.title_text": spec.x_axis_title,
        "graph.y_axis.title_text": spec.y_axis_title,
    }


def _legacy_card_candidate(
    client: MetabaseClient,
    collection_id: int,
    spec: WhoopCardSpec,
    suffix: str,
) -> dict[str, Any] | None:
    return _card_snapshot_candidate(client, collection_id, spec, suffix, legacy=True)


def _card_snapshot_candidate(
    client: MetabaseClient,
    collection_id: int,
    spec: WhoopCardSpec,
    suffix: str,
    *,
    legacy: bool,
) -> dict[str, Any] | None:
    candidates = [
        card
        for card in _rows(client, "/api/card")
        if card.get("name") == f"{spec.name}{suffix}"
        and card.get("collection_id") == collection_id
        and not card.get("archived")
    ]
    if len(candidates) != 1:
        return None
    card = candidates[0]
    _, query = _native_query(card.get("dataset_query"))
    expected_visualization = (
        _legacy_visualization_settings(spec)
        if legacy
        else _visualization_settings(spec)
    )
    if (
        card.get("display") != spec.display
        or query != spec.query
        or card.get("visualization_settings") != expected_visualization
        or (not legacy and card.get("description") != spec.description)
    ):
        return None
    return card


def _legacy_visualization_settings(spec: WhoopCardSpec) -> dict[str, Any]:
    return {
        "graph.dimensions": ["date"],
        "graph.metrics": list(spec.metrics),
    }


def _managed_card_layouts(
    client: MetabaseClient,
    dashboard_id: int,
    managed_card_ids: set[int],
) -> tuple[tuple[int, int], ...]:
    dashboard = client.request("GET", f"/api/dashboard/{dashboard_id}")
    if not isinstance(dashboard, dict) or not isinstance(
        dashboard.get("dashcards", []), list
    ):
        raise TypeError("Unexpected Metabase dashboard response")
    occupied = [
        _dashcard_rectangle(item)
        for item in dashboard.get("dashcards", [])
        if isinstance(item, dict) and item.get("card_id") not in managed_card_ids
    ]
    layouts: list[tuple[int, int]] = []
    slot = 0
    while len(layouts) < len(managed_card_ids):
        row, col = (slot // 2) * 8, (slot % 2) * 12
        rectangle = (row, col, 8, 12)
        if all(not _rectangles_overlap(rectangle, other) for other in occupied):
            layouts.append((row, col))
            occupied.append(rectangle)
        slot += 1
    return tuple(layouts)


def _dashcard_rectangle(item: dict[str, Any]) -> tuple[int, int, int, int]:
    row = item.get("row")
    col = item.get("col")
    size_y = item.get("size_y")
    size_x = item.get("size_x")
    if not all(isinstance(value, int) for value in (row, col, size_y, size_x)):
        return (0, 0, 8, 24)
    assert isinstance(row, int)
    assert isinstance(col, int)
    assert isinstance(size_y, int)
    assert isinstance(size_x, int)
    return row, col, size_y, size_x


def _rectangles_overlap(
    first: tuple[int, int, int, int], second: tuple[int, int, int, int]
) -> bool:
    first_row, first_col, first_height, first_width = first
    second_row, second_col, second_height, second_width = second
    return (
        first_row < second_row + second_height
        and second_row < first_row + first_height
        and first_col < second_col + second_width
        and second_col < first_col + first_width
    )


def _legacy_whoop_card_specs(profile_id: UUID) -> tuple[WhoopCardSpec, ...]:
    """Return the exact v0.1 card snapshots used only for ownership checks."""
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
