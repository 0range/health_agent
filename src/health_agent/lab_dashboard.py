"""Profile-owned lab history: one registered source-unit family per chart."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import Engine, text

from health_agent.config import Settings
from health_agent.db import build_engine
from health_agent.lab_extraction.registry import _ANALYTES, _UNITS, normalize_registered
from health_agent.metabase import (
    MetabaseClient,
    _ensure_collection,
    _ensure_dashboard_card,
    _ensure_database,
    _native_query,
    _require_entity,
    _rows,
    ensure_dashboard_reader,
)


@dataclass(frozen=True)
class LabSeries:
    canonical_name: str
    label: str
    unit: str


@dataclass(frozen=True)
class LabCardSpec:
    name: str
    query: str
    metrics: tuple[str, ...]
    display: str
    description: str
    unit: str = ""


@dataclass(frozen=True)
class LabDashboardResult:
    dashboard_id: int
    card_ids: tuple[int, ...]
    dashboard_url: str


DEFAULT_SERIES = (
    LabSeries("ferritin", "Ферритин", "ng/mL"),
    LabSeries("vitamin_b12", "Витамин B12", "pg/mL"),
    LabSeries("folate", "Фолат", "ng/mL"),
    LabSeries("vitamin_d", "Витамин D", "ng/mL"),
    LabSeries("total_cholesterol", "Общий холестерин", "mmol/L"),
    LabSeries("ldl_cholesterol", "Холестерин ЛПНП", "mmol/L"),
    LabSeries("hdl_cholesterol", "Холестерин ЛПВП", "mmol/L"),
    LabSeries("triglycerides", "Триглицериды", "mmol/L"),
    LabSeries("iron", "Железо", "umol/L"),
    LabSeries("prolactin", "Пролактин", "ng/mL"),
    LabSeries("hemoglobin", "Гемоглобин", "g/L"),
    LabSeries("glucose", "Глюкоза", "mmol/L"),
    LabSeries("tsh", "ТТГ", "mIU/L"),
)
_LABELS = {
    name: next((a for a in aliases.split("|") if any("А" <= c <= "я" for c in a)), name)
    for name, aliases, _ in _ANALYTES
} | {series.canonical_name: series.label for series in DEFAULT_SERIES}
_MAX_SERIES = 80
_OWNER = "health-agent:lab-history:v1"
# Bound syntax before casts, including exponents, so hostile stored text cannot
# cause PostgreSQL numeric overflow even when the planner reorders predicates.
_NUMBER = (
    r"[+-]?(?:[0-9]{1,64}(?:[.,][0-9]{1,64})?|[.,][0-9]{1,64})(?:[eE][+-]?[0-9]{1,2})?"
)


def _literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _profile(profile_id: UUID) -> str:
    if not isinstance(profile_id, UUID):
        raise TypeError("profile_id must be a UUID")
    return str(profile_id)


def _history_cte(
    profile_id: UUID, series: LabSeries | None = None, *, legacy: bool = False
) -> str:
    """Use the registry itself, not a second hand-maintained unit allowlist."""
    profile = _profile(profile_id)
    entries = []
    for name, _, units in _ANALYTES:
        for raw_unit, unit in sorted(_UNITS.items()):
            if legacy and raw_unit == "пг/кл":
                continue
            if unit not in units.split("|"):
                continue
            if series is not None and (name, unit) != (
                series.canonical_name,
                series.unit,
            ):
                continue
            entries.append(
                "("
                + ", ".join(map(_literal, (name, raw_unit, unit, _LABELS[name])))
                + ")"
            )
    registry = ",\n".join(entries)
    return f"""-- {_OWNER} [{profile}]
WITH registry(canonical_name, source_unit_key, unit, label) AS (VALUES {registry}),
source_rows AS (
  SELECT h.*, o.source_flag, r.unit AS chart_unit, r.label,
    CASE WHEN h.source_value ~ {_literal("^" + _NUMBER + "$")}
      THEN replace(h.source_value, ',', '.')::numeric END AS exact_value,
    regexp_match(h.reference_text,
      {_literal("^(" + _NUMBER + ")[[:blank:]]*[-–—][[:blank:]]*(" + _NUMBER + ")$")}) AS printed_range
  FROM verified_lab_history h
  JOIN lab_observations o ON o.id = h.id
  JOIN registry r ON r.canonical_name = h.canonical_name
    AND r.source_unit_key = replace(lower(btrim(h.source_unit)), 'μ', 'µ')
  WHERE h.profile_id = '{profile}'
    AND h.document_processing_status = 'processed'
    AND h.document_safe_error_code IS NULL
    AND h.result_date IS NOT NULL AND h.result_date <= CURRENT_DATE
    AND h.parsed_value BETWEEN -1e12 AND 1e12
    AND h.normalized_value BETWEEN -1e12 AND 1e12
    AND scale(h.parsed_value) <= 12 AND scale(h.normalized_value) <= 12
    AND length(h.source_value) <= 64
    AND h.normalized_unit IS NOT NULL
), valid_rows AS (
  SELECT *, CASE WHEN reference_low BETWEEN -1e12 AND 1e12
    AND reference_high BETWEEN -1e12 AND 1e12 AND reference_low <= reference_high
    AND scale(reference_low) <= 12 AND scale(reference_high) <= 12
    AND reference_low = replace(printed_range[1], ',', '.')::numeric
    AND reference_high = replace(printed_range[2], ',', '.')::numeric
    THEN true ELSE false END AS compatible_range
  FROM source_rows WHERE exact_value = parsed_value
)
"""


def lab_card_specs(
    profile_id: UUID, series: tuple[LabSeries, ...], *, _legacy: bool = False
) -> tuple[LabCardSpec, ...]:
    profile = _profile(profile_id)
    if len(series) > _MAX_SERIES or len(
        {(s.canonical_name, s.unit) for s in series}
    ) != len(series):
        raise ValueError("Lab series must be unique and bounded to 80")
    for item in series:
        if normalize_registered(item.canonical_name, "1", item.unit)[1] != item.unit:
            raise ValueError("Lab series requires a canonical registry unit")
    detail = LabCardSpec(
        f"Анализы — исходные данные [{profile}]",
        _history_cte(profile_id, legacy=_legacy)
        + """SELECT result_date AS date, label AS analyte,
  canonical_name, source_name, source_value, source_unit, reference_text, source_flag,
  CASE WHEN NOT compatible_range THEN 'unknown'
       WHEN parsed_value < reference_low THEN 'below'
       WHEN parsed_value > reference_high THEN 'above' ELSE 'within' END AS comparison,
  document_id, page_number, id AS observation_id
FROM valid_rows ORDER BY result_date DESC, canonical_name, document_id, page_number, id
LIMIT 1000""",
        (),
        "table",
        "Последние 1000 подтверждённых датированных значений этого профиля. "
        "Пустая таблица означает: подтверждённых датированных результатов пока нет. "
        "Точный результат, единица, референс и флаг из источника; документ и страница. "
        "below/within/above — только числовое сравнение с напечатанным двухсторонним "
        "референсом в той же единице; иначе unknown. Это не диагноз.",
    )
    charts = tuple(
        LabCardSpec(
            f"{item.label} — {item.unit} [{profile}]",
            _history_cte(profile_id, item, legacy=_legacy)
            + (
                """SELECT result_date AS date,
  parsed_value AS result,
  CASE WHEN compatible_range THEN reference_low END AS reference_low,
  CASE WHEN compatible_range THEN reference_high END AS reference_high,
  id AS observation_id, document_id, page_number
FROM valid_rows ORDER BY result_date, document_id, page_number, id"""
                if _legacy
                else """SELECT result_date AS date,
  CASE WHEN count(*) OVER (PARTITION BY result_date) > 1
    THEN to_char(result_date, 'YYYY-MM-DD') || ' · ' ||
      row_number() OVER (PARTITION BY result_date ORDER BY document_id, page_number, id)::text
    ELSE to_char(result_date, 'YYYY-MM-DD') END AS date_label,
  parsed_value AS result,
  CASE WHEN compatible_range THEN reference_low END AS reference_low,
  CASE WHEN compatible_range THEN reference_high END AS reference_high,
  id AS observation_id, document_id, page_number
FROM valid_rows ORDER BY result_date, document_id, page_number, id"""
            ),
            ("result", "reference_low", "reference_high"),
            "line",
            f"{item.label}, {item.unit}: вся история без усреднения за день. "
            "Пустой график означает: нет подтверждённых датированных значений. "
            "Границы — напечатанный двухсторонний референс источника, не диагноз. "
            "Точные строки и происхождение — в таблице «Анализы — исходные данные» "
            "на этом дашборде. Единицы не конвертируются.",
            item.unit,
        )
        for item in series
    )
    return (detail, *charts)


def discover_lab_series(engine: Engine, profile_id: UUID) -> tuple[LabSeries, ...]:
    query = (
        _history_cte(profile_id)
        + """SELECT DISTINCT canonical_name, label, chart_unit
FROM valid_rows ORDER BY canonical_name, chart_unit LIMIT 80"""
    )
    with engine.connect() as connection:
        rows = connection.execute(text(query)).all()
    available = {(row[0], row[2]): LabSeries(*row) for row in rows}
    ordered = [
        available.pop((item.canonical_name, item.unit))
        for item in DEFAULT_SERIES
        if (item.canonical_name, item.unit) in available
    ]
    ordered.extend(available[key] for key in sorted(available))
    return tuple(ordered[:_MAX_SERIES])


def _visualization(spec: LabCardSpec) -> dict[str, Any]:
    if spec.display == "table":
        return {
            "column_settings": {
                '["name","date"]': {"column_title": "Дата"},
                '["name","analyte"]': {"column_title": "Исследование"},
                '["name","source_value"]': {"column_title": "Результат"},
                '["name","source_unit"]': {"column_title": "Единица"},
                '["name","reference_text"]': {"column_title": "Референс"},
                '["name","source_flag"]': {"column_title": "Флаг источника"},
                '["name","comparison"]': {
                    "column_title": "Сравнение",
                    "value_remappings": [
                        {"value": "below", "new_value": "Ниже референса"},
                        {"value": "within", "new_value": "В референсе"},
                        {"value": "above", "new_value": "Выше референса"},
                        {"value": "unknown", "new_value": "Не определено"},
                    ],
                },
            }
        }
    return {
        "graph.dimensions": ["date_label"],
        "graph.metrics": list(spec.metrics),
        "graph.x_axis.title_text": "Дата · отдельные измерения",
        "graph.x_axis.scale": "ordinal",
        "graph.y_axis.title_text": spec.unit,
        "graph.show_dots": True,
        "graph.show_values": False,
        "graph.missing": "none",
        "series_settings": {
            "result": {"title": "Результат"},
            "reference_low": {"title": "Нижняя граница референса"},
            "reference_high": {"title": "Верхняя граница референса"},
        },
    }


def bootstrap_lab_dashboard(
    settings: Settings,
    profile_id: UUID,
    *,
    transport: httpx.BaseTransport | None = None,
    engine: Engine | None = None,
) -> LabDashboardResult:
    """Provision only owned objects; never adopt cards by name alone."""
    profile = _profile(profile_id)
    database_engine = engine if engine is not None else build_engine(settings)
    try:
        series = discover_lab_series(database_engine, profile_id)
        specs = lab_card_specs(profile_id, series)
        ensure_dashboard_reader(settings, engine=database_engine)
    finally:
        if engine is None:
            database_engine.dispose()
    with MetabaseClient(settings.metabase_url, transport=transport) as client:
        client.wait_until_healthy()
        client.authenticate(
            settings.effective_metabase_admin_email, settings.postgres_password
        )
        collection = _ensure_collection(client)
        database = _ensure_database(client, settings)
        dashboard_name = f"Анализы крови — история [{profile}]"
        marker = f"{_OWNER} [{profile}]"
        matches = [
            d
            for d in _rows(client, "/api/dashboard")
            if d.get("name") == dashboard_name
        ]
        if len(matches) > 1 or any(
            d.get("collection_id") != collection["id"] or d.get("description") != marker
            for d in matches
        ):
            raise ValueError("Lab dashboard ownership collision")

        # Preflight every matching card before changing any cards or dashboard.
        cards = _rows(client, "/api/card")
        owned: list[dict[str, Any] | None] = []
        legacy_specs = lab_card_specs(profile_id, series, _legacy=True)
        for spec, legacy_spec in zip(specs, legacy_specs, strict=True):
            candidates = [c for c in cards if c.get("name") == spec.name]
            if len(candidates) > 1:
                raise ValueError("Lab card ownership collision")
            current = None
            if candidates:
                current = _require_entity(
                    client.request("GET", f"/api/card/{candidates[0]['id']}"), "card"
                )
                if (
                    current.get("name") != spec.name
                    or current.get("collection_id") != collection["id"]
                    or _native_query(current.get("dataset_query"))
                    not in {
                        (database["id"], spec.query),
                        (database["id"], legacy_spec.query),
                    }
                ):
                    raise ValueError("Lab card ownership collision")
            owned.append(current)

        dashboard = (
            matches[0]
            if matches
            else _require_entity(
                client.request(
                    "POST",
                    "/api/dashboard",
                    json={
                        "name": dashboard_name,
                        "description": marker,
                        "collection_id": collection["id"],
                    },
                ),
                "dashboard",
            )
        )
        _detach_unselected_owned_cards(
            client,
            dashboard["id"],
            collection["id"],
            database["id"],
            profile_id,
            specs,
            cards,
        )
        ids = []
        for spec, current in zip(specs, owned, strict=True):
            payload = {
                "name": spec.name,
                "collection_id": collection["id"],
                "display": spec.display,
                "description": spec.description,
                "dataset_query": {
                    "database": database["id"],
                    "type": "native",
                    "native": {"query": spec.query},
                },
                "visualization_settings": _visualization(spec),
            }
            card = _require_entity(
                client.request(
                    "PUT" if current else "POST",
                    f"/api/card/{current['id']}" if current else "/api/card",
                    json=payload,
                ),
                "card",
            )
            ids.append(card["id"])
        for index, card_id in enumerate(ids):
            _ensure_dashboard_card(
                client,
                dashboard["id"],
                card_id,
                row=0 if index == 0 else 8 + ((index - 1) // 2) * 8,
                col=0 if index == 0 else ((index - 1) % 2) * 12,
                size_x=24 if index == 0 else 12,
                size_y=8,
            )
        _place_user_cards_below_history(client, dashboard["id"], ids)
    return LabDashboardResult(
        dashboard["id"],
        tuple(ids),
        f"{settings.metabase_url.rstrip('/')}/dashboard/{dashboard['id']}",
    )


def _place_user_cards_below_history(
    client: MetabaseClient, dashboard_id: int, owned_ids: list[int]
) -> None:
    """Preserve user cards and settings, shifting their group only if it overlaps."""
    dashboard = _require_entity(
        client.request("GET", f"/api/dashboard/{dashboard_id}"), "dashboard"
    )
    dashcards = dashboard.get("dashcards")
    if not isinstance(dashcards, list) or not all(
        isinstance(c, dict) for c in dashcards
    ):
        raise TypeError("Unexpected Metabase dashboard cards response")
    user_cards = [c for c in dashcards if c.get("card_id") not in owned_ids]
    end_row = 8 + (len(owned_ids) // 2) * 8
    if not user_cards:
        return
    first_row = min(c.get("row", 0) for c in user_cards)
    if first_row >= end_row:
        return
    shift = end_row - first_row
    client.request(
        "PUT",
        f"/api/dashboard/{dashboard_id}",
        json={
            "dashcards": [
                {**c, "row": c.get("row", 0) + shift}
                if c.get("card_id") not in owned_ids
                else c
                for c in dashcards
            ]
        },
    )


def _detach_unselected_owned_cards(
    client: MetabaseClient,
    dashboard_id: int,
    collection_id: int,
    database_id: int,
    profile_id: UUID,
    selected: tuple[LabCardSpec, ...],
    cards: list[dict[str, Any]],
) -> None:
    """Keep the active history bounded as discovery changes; never delete cards."""
    names = {spec.name for spec in selected}
    possible = {
        f"{_LABELS[name]} — {unit} [{profile_id}]": LabSeries(name, _LABELS[name], unit)
        for name, _, units in _ANALYTES
        for unit in units.split("|")
    }
    retired = set()
    for card in cards:
        name = card.get("name")
        if name in names or name not in possible:
            continue
        current = _require_entity(
            client.request("GET", f"/api/card/{card['id']}"), "card"
        )
        expected = lab_card_specs(profile_id, (possible[name],))[1]
        legacy_expected = lab_card_specs(profile_id, (possible[name],), _legacy=True)[1]
        if (
            current.get("name") == name
            and current.get("collection_id") == collection_id
            and _native_query(current.get("dataset_query"))
            in {
                (database_id, expected.query),
                (database_id, legacy_expected.query),
            }
        ):
            retired.add(card["id"])
    if retired:
        dashboard = _require_entity(
            client.request("GET", f"/api/dashboard/{dashboard_id}"), "dashboard"
        )
        dashcards = dashboard.get("dashcards")
        if not isinstance(dashcards, list) or not all(
            isinstance(c, dict) for c in dashcards
        ):
            raise TypeError("Unexpected Metabase dashboard cards response")
        remaining = [c for c in dashcards if c.get("card_id") not in retired]
        if remaining != dashcards:
            client.request(
                "PUT", f"/api/dashboard/{dashboard_id}", json={"dashcards": remaining}
            )
