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
    bootstrap_whoop_dashboard,
    whoop_card_specs,
)

PROFILE = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")


def test_queries_are_bound_to_exact_profile_and_do_not_invent_zeroes() -> None:
    specs = whoop_card_specs(PROFILE)

    assert len(specs) == 5
    for spec in specs:
        assert str(PROFILE) in spec.query
        assert "coalesce" not in spec.query.lower()
    assert "whoop_daily_health" in specs[0].query
    assert "whoop_sleep_history" in specs[2].query
    assert "whoop_body_snapshot" in specs[4].query


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
    assert fake.count_named(f"{WHOOP_DASHBOARD_NAME} [{str(PROFILE)[:8]}]") == 1
    assert len(fake.cards) == 5
    assert len(fake.dashboards[0]["dashcards"]) == 5
    for card in fake.cards:
        query = card["dataset_query"]["native"]["query"]
        assert str(PROFILE) in query
        assert card["visualization_settings"]["graph.dimensions"] == ["date"]


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
    assert len(fake.cards) == 10
    for card in fake.cards[:5]:
        assert str(PROFILE) in card["dataset_query"]["native"]["query"]
        assert str(other) not in card["dataset_query"]["native"]["query"]
    for card in fake.cards[5:]:
        assert str(other) in card["dataset_query"]["native"]["query"]
        assert str(PROFILE) not in card["dataset_query"]["native"]["query"]


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
        "bootstrap_whoop_dashboard",
        lambda _settings, _profile_id: WhoopDashboardResult(
            dashboard_id=7,
            card_ids=(1, 2, 3, 4, 5),
            dashboard_url="http://127.0.0.1:53000/dashboard/7",
        ),
    )

    result = CliRunner().invoke(
        cli.app, ["dashboard", "setup-whoop", "--profile-id", str(PROFILE)]
    )

    assert result.exit_code == 0
    assert result.stdout == (
        f"status=ready profile_id={PROFILE} dashboard_id=7 cards=5 "
        "url=http://127.0.0.1:53000/dashboard/7\n"
    )
