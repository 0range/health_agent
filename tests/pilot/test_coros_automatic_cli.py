from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from typer.testing import CliRunner

from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.cli import app
from health_agent.pilot.coros_auth import CorosAuthError


@pytest.mark.parametrize("attempted", [True, False])
def test_automatic_coros_uses_daily_gate_without_full_history(monkeypatch, attempted):
    engine = Mock()
    store = SimpleNamespace(engine=engine)
    gate = Mock(return_value=attempted)
    full_sync = Mock(side_effect=AssertionError("must not run a full sync"))
    monkeypatch.setattr("health_agent.pilot.cli.Settings", lambda: object())
    monkeypatch.setattr("health_agent.pilot.cli.build_engine", lambda _: engine)
    monkeypatch.setattr("health_agent.pilot.cli.PilotStore", lambda _: store)
    monkeypatch.setattr("health_agent.pilot.runtime.maybe_coros_sync", gate)
    monkeypatch.setattr("health_agent.pilot.coros_sync.run_coros_sync", full_sync)

    result = CliRunner().invoke(app, ["coros-sync", "--automatic"])

    assert result.exit_code == 0, result.output
    assert f"attempted={str(attempted).lower()}" in result.output
    gate.assert_called_once()
    assert gate.call_args.args[:2] == (store, DEFAULT_PROFILE_ID)
    assert gate.call_args.args[2].name == str(DEFAULT_PROFILE_ID)
    assert gate.call_args.args[3].tzinfo is not None
    full_sync.assert_not_called()
    engine.dispose.assert_called_once()


def test_automatic_coros_reports_auth_failure_without_provider_content(monkeypatch):
    engine = Mock()
    monkeypatch.setattr("health_agent.pilot.cli.Settings", lambda: object())
    monkeypatch.setattr("health_agent.pilot.cli.build_engine", lambda _: engine)
    monkeypatch.setattr(
        "health_agent.pilot.cli.PilotStore", lambda _: SimpleNamespace(engine=engine)
    )
    monkeypatch.setattr(
        "health_agent.pilot.runtime.maybe_coros_sync",
        Mock(side_effect=CorosAuthError("private provider response")),
    )

    result = CliRunner().invoke(app, ["coros-sync", "--automatic"])

    assert result.exit_code == 1
    assert "CorosAuthError" in result.output
    assert "private provider response" not in result.output
    engine.dispose.assert_called_once()


def test_automatic_coros_rejects_conflicting_date_bounds(monkeypatch):
    build = Mock(side_effect=AssertionError("must not open the database"))
    monkeypatch.setattr("health_agent.pilot.cli.build_engine", build)

    result = CliRunner().invoke(
        app, ["coros-sync", "--automatic", "--since", "2026-10-01"]
    )

    assert result.exit_code == 2
    assert "cannot be combined" in result.output
    build.assert_not_called()
