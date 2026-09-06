from __future__ import annotations

from typing import cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import Engine
from test_metabase import FakeEngine, FakeMetabase
from typer.testing import CliRunner

from health_agent import cli
from health_agent.config import Settings
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.whoop.dashboard import (
    WHOOP_DASHBOARD_NAME,
    WhoopDashboardResult,
    _legacy_whoop_card_specs,
    bootstrap_whoop_dashboard,
    whoop_card_specs,
)

PROFILE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_queries_are_bound_to_exact_profile_and_do_not_invent_zeroes() -> None:
    specs = whoop_card_specs(PROFILE)

    assert len(specs) == 8
    for spec in specs:
        assert str(PROFILE) in spec.query
        assert "coalesce" not in spec.query.lower()
    assert "whoop_cycles" in specs[0].query
    assert "whoop_sleeps" in specs[4].query
    assert "whoop_body_snapshot" in specs[7].query


def test_one_metric_per_chart_and_weight_is_not_a_trend() -> None:
    specs = whoop_card_specs(PROFILE)
    assert len(specs) == 8
    charts = [spec for spec in specs if spec.display == "line"]
    assert len(charts) == 7
    assert all(len(spec.metrics) == 1 for spec in charts)
    weight = next(spec for spec in specs if spec.display == "table")
    assert "observed_at" in weight.query
    assert "LIMIT 1" in weight.query


def test_specs_cover_exact_metrics_with_metric_specific_copy() -> None:
    specs = whoop_card_specs(PROFILE)
    expected = {
        "recovery_score": ("WHOOP — Recovery", "%", "Recovery"),
        "strain": ("WHOOP — strain", "0–21", "Strain"),
        "hrv_rmssd_milli": ("WHOOP — HRV", "мс", "HRV"),
        "resting_heart_rate": ("WHOOP — пульс покоя", "уд/мин", "Пульс покоя"),
        "sleep_hours": ("WHOOP — длительность сна", "ч", "Длительность"),
        "sleep_performance_percentage": (
            "WHOOP — выполнение потребности во сне",
            "%",
            "потребности во сне",
        ),
        "sleep_efficiency_percentage": (
            "WHOOP — эффективность сна",
            "%",
            "Эффективность",
        ),
        "weight_kilogram": ("WHOOP — вес", "кг", "веса"),
    }
    assert {spec.metrics[0] for spec in specs} == set(expected)
    for spec in specs:
        name, unit, description_term = expected[spec.metrics[0]]
        assert (spec.name, spec.unit) == (name, unit)
        assert description_term in spec.description
        assert "не диагноз" in spec.description
        assert "причин" in spec.description
        if spec.display == "line":
            assert spec.x_axis_title == "Дата"
            assert unit in spec.y_axis_title


def test_whoop_dashboard_is_profile_isolated_and_idempotent() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())

    first = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )
    second = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )

    assert first == second
    assert fake.count_named(f"{WHOOP_DASHBOARD_NAME} [{PROFILE}]") == 1
    assert len(fake.cards) == 8
    assert len(fake.dashboards[0]["dashcards"]) == 8
    for card in fake.cards:
        query = card["dataset_query"]["native"]["query"]
        assert str(PROFILE) in query
        if card["display"] == "line":
            assert card["visualization_settings"]["graph.dimensions"] == ["date"]
            assert card["visualization_settings"]["graph.x_axis.title_text"] == "Дата"
        else:
            assert card["display"] == "table"
            assert not any(
                key.startswith("graph.") for key in card["visualization_settings"]
            )
            assert card["visualization_settings"]["column_settings"] == {
                '["name","weight_kilogram"]': {"column_title": "Вес, кг"},
                '["name","observed_at"]': {"column_title": "Получено из WHOOP"},
            }


def test_two_profiles_get_separate_dashboards_and_cards() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())
    other = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")

    first = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )
    second = bootstrap_whoop_dashboard(
        settings, other, transport=transport, engine=engine
    )

    assert first.dashboard_id != second.dashboard_id
    assert len(fake.dashboards) == 2
    assert len(fake.cards) == 16
    for card in fake.cards[:8]:
        assert str(PROFILE) in card["dataset_query"]["native"]["query"]
        assert str(other) not in card["dataset_query"]["native"]["query"]
    for card in fake.cards[8:]:
        assert str(other) in card["dataset_query"]["native"]["query"]
        assert str(PROFILE) not in card["dataset_query"]["native"]["query"]


def test_profiles_with_same_uuid_prefix_never_share_objects() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())
    first_profile = UUID("aaaaaaaa-1111-4111-8111-111111111111")
    second_profile = UUID("aaaaaaaa-2222-4222-8222-222222222222")

    first = bootstrap_whoop_dashboard(
        settings, first_profile, transport=transport, engine=engine
    )
    second = bootstrap_whoop_dashboard(
        settings, second_profile, transport=transport, engine=engine
    )

    assert first.dashboard_id != second.dashboard_id
    assert len(fake.dashboards) == 2
    assert len(fake.cards) == 16
    assert all(
        str(first_profile) in card["dataset_query"]["native"]["query"]
        for card in fake.cards[:8]
    )
    assert all(
        str(second_profile) in card["dataset_query"]["native"]["query"]
        for card in fake.cards[8:]
    )


def test_non_default_legacy_short_names_are_reused_without_duplicates() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())
    legacy_suffix = f" [{str(PROFILE)[:8]}]"

    legacy_ids = _seed_legacy_dashboard(fake, PROFILE, legacy_suffix)

    result = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )

    assert result.dashboard_id == 1
    assert tuple(result.card_ids[index] for index in (0, 2, 4, 5, 7)) == legacy_ids
    assert len(fake.dashboards) == 1
    assert len(fake.cards) == 8
    assert fake.dashboards[0]["name"] == f"{WHOOP_DASHBOARD_NAME} [{PROFILE}]"


def test_short_names_with_current_shape_are_reused_without_duplicates() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())
    legacy_suffix = f" [{str(PROFILE)[:8]}]"

    first = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )
    fake.dashboards[0]["name"] = f"{WHOOP_DASHBOARD_NAME}{legacy_suffix}"
    for card, spec in zip(fake.cards, whoop_card_specs(PROFILE), strict=True):
        card["name"] = f"{spec.name}{legacy_suffix}"

    second = bootstrap_whoop_dashboard(
        settings, PROFILE, transport=transport, engine=engine
    )

    assert second == first
    assert len(fake.dashboards) == 1
    assert len(fake.cards) == 8


def test_same_prefix_profile_cannot_claim_foreign_legacy_objects() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())
    first_profile = UUID("aaaaaaaa-1111-4111-8111-111111111111")
    second_profile = UUID("aaaaaaaa-2222-4222-8222-222222222222")
    legacy_suffix = " [aaaaaaaa]"

    legacy_ids = _seed_legacy_dashboard(fake, first_profile, legacy_suffix)

    second = bootstrap_whoop_dashboard(
        settings, second_profile, transport=transport, engine=engine
    )

    assert second.dashboard_id != 1
    assert len(fake.dashboards) == 2
    assert len(fake.cards) == 13
    assert all(
        str(first_profile) in card["dataset_query"]["native"]["query"]
        for card in fake.cards
        if card["id"] in legacy_ids
    )
    assert all(
        str(second_profile) in card["dataset_query"]["native"]["query"]
        for card in fake.cards
        if card["id"] in second.card_ids
    )


def test_old_five_card_upgrade_preserves_ids_and_user_card() -> None:
    fake = FakeMetabase()
    suffix = f" [{PROFILE}]"
    legacy_ids = _seed_legacy_dashboard(fake, PROFILE, suffix)
    user_card = {
        "id": 99,
        "name": "Моя заметка",
        "collection_id": 1,
        "display": "table",
        "dataset_query": {
            "database": 1,
            "type": "native",
            "native": {"query": "SELECT 1"},
        },
        "visualization_settings": {},
    }
    fake.cards.append(user_card)
    user_layout = {
        "id": 99,
        "card_id": 99,
        "row": 0,
        "col": 0,
        "size_x": 12,
        "size_y": 8,
        "parameter_mappings": [],
        "visualization_settings": {},
    }
    fake.dashboards[0]["dashcards"].append(user_layout.copy())

    result = bootstrap_whoop_dashboard(
        Settings(postgres_password="local-secret"),
        PROFILE,
        transport=httpx.MockTransport(fake.handle),
        engine=cast(Engine, FakeEngine()),
    )

    assert result.dashboard_id == 1
    assert tuple(result.card_ids[index] for index in (0, 2, 4, 5, 7)) == legacy_ids
    assert len(result.card_ids) == 8
    assert len(fake.cards) == 9
    assert (
        next(item for item in fake.dashboards[0]["dashcards"] if item["card_id"] == 99)
        == user_layout
    )
    managed = [
        item
        for item in fake.dashboards[0]["dashcards"]
        if item["card_id"] in result.card_ids
    ]
    assert all(not (item["row"] == 0 and item["col"] == 0) for item in managed)
    assert all(request.method != "DELETE" for request in fake.requests)


def test_whoop_card_specs_rejects_non_uuid_input() -> None:
    with pytest.raises((TypeError, ValueError)):
        whoop_card_specs(cast(UUID, str(PROFILE)))


def test_existing_dashboard_card_layout_drift_is_repaired() -> None:
    fake = FakeMetabase()
    transport = httpx.MockTransport(fake.handle)
    settings = Settings(postgres_password="local-secret")
    engine = cast(Engine, FakeEngine())

    bootstrap_whoop_dashboard(settings, PROFILE, transport=transport, engine=engine)
    fake.dashboards[0]["dashcards"][0].update(
        {"row": 99, "col": 7, "size_x": 1, "size_y": 1}
    )

    bootstrap_whoop_dashboard(settings, PROFILE, transport=transport, engine=engine)

    first_card = fake.dashboards[0]["dashcards"][0]
    assert (first_card["row"], first_card["col"]) == (0, 0)
    assert (first_card["size_x"], first_card["size_y"]) == (12, 8)


def test_default_profile_has_clean_visible_names() -> None:
    fake = FakeMetabase()
    result = bootstrap_whoop_dashboard(
        Settings(postgres_password="local-secret"),
        DEFAULT_PROFILE_ID,
        transport=httpx.MockTransport(fake.handle),
        engine=cast(Engine, FakeEngine()),
    )

    assert result.dashboard_id == 1
    assert fake.dashboards[0]["name"] == WHOOP_DASHBOARD_NAME
    assert all("[" not in card["name"] for card in fake.cards)


def test_setup_whoop_cli_prints_safe_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_profile_exists",
        lambda _settings, _profile_id: True,
    )
    monkeypatch.setattr(
        cli,
        "bootstrap_whoop_dashboard",
        lambda _settings, _profile_id: WhoopDashboardResult(
            dashboard_id=7,
            card_ids=(1, 2, 3, 4, 5, 6, 7, 8),
            dashboard_url="http://127.0.0.1:53000/dashboard/7",
        ),
    )

    result = CliRunner().invoke(
        cli.app, ["dashboard", "setup-whoop", "--profile-id", str(PROFILE)]
    )

    assert result.exit_code == 0
    assert result.stdout == (
        f"status=ready profile_id={PROFILE} dashboard_id=7 cards=8 "
        "url=http://127.0.0.1:53000/dashboard/7\n"
    )


def test_setup_whoop_cli_rejects_unknown_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cli,
        "_profile_exists",
        lambda _settings, _profile_id: False,
    )

    result = CliRunner().invoke(
        cli.app, ["dashboard", "setup-whoop", "--profile-id", str(PROFILE)]
    )

    assert result.exit_code != 0
    assert "profile does not exist" in result.output


def _seed_legacy_dashboard(
    fake: FakeMetabase, profile_id: UUID, suffix: str
) -> tuple[int, ...]:
    fake.collections.append(
        {"id": 1, "name": "Health Agent", "parent_id": None, "location": "/"}
    )
    fake.databases.append(
        {
            "id": 1,
            "name": "Health Agent",
            "engine": "postgres",
            "details": {
                "host": "postgres",
                "port": 5432,
                "dbname": "health_agent",
                "user": "health_dashboard",
                "password": "local-secret",
                "ssl": False,
            },
        }
    )
    specs = _legacy_whoop_card_specs(profile_id)
    cards = []
    dashcards = []
    for card_id, spec in enumerate(specs, 1):
        cards.append(
            {
                "id": card_id,
                "name": f"{spec.name}{suffix}",
                "collection_id": 1,
                "display": "line",
                "dataset_query": {
                    "database": 1,
                    "type": "native",
                    "native": {"query": spec.query, "template-tags": {}},
                },
                "visualization_settings": {
                    "graph.dimensions": ["date"],
                    "graph.metrics": list(spec.metrics),
                },
            }
        )
        dashcards.append(
            {
                "id": card_id,
                "card_id": card_id,
                "row": ((card_id - 1) // 2) * 8,
                "col": ((card_id - 1) % 2) * 12,
                "size_x": 12,
                "size_y": 8,
                "parameter_mappings": [],
                "visualization_settings": {},
            }
        )
    fake.cards.extend(cards)
    fake.dashboards.append(
        {
            "id": 1,
            "name": f"{WHOOP_DASHBOARD_NAME}{suffix}",
            "collection_id": 1,
            "dashcards": dashcards,
        }
    )
    return tuple(range(1, 6))
