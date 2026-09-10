import base64
import json
import plistlib
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from health_agent.automation.launchd import LaunchdPaths
from health_agent.automation.storage import atomic_private_write
from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID
from health_agent.pilot.storage import PilotStore
from health_agent.research.cli import LABEL as QUALITY_LABEL
from health_agent.research.cli import QualityLaunchdManager
from health_agent.whoop.details import DetailClient, DetailError, DetailService
from health_agent.whoop.details_cli import LABEL as DETAIL_LABEL
from health_agent.whoop.details_cli import DetailLaunchdManager
from health_agent.whoop.models import WhoopConnection


def token(user=7, exp=4000000000):
    body = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"custom:user_id": user, "client_id": "synthetic-client", "exp": exp}
            ).encode()
        )
        .decode()
        .rstrip("=")
    )
    return "header." + body + ".synthetic"


def client(tmp_path, handler):
    path = tmp_path / "session.json"
    atomic_private_write(
        path,
        json.dumps(
            {
                "profile_id": str(DEFAULT_PROFILE_ID),
                "user_id": 7,
                "whoop-auth-token": token(),
                "whoop-auth-refresh-token": "synthetic-refresh",
            }
        ).encode(),
    )
    return DetailClient(httpx.Client(transport=httpx.MockTransport(handler)), path)


def test_refresh_preserves_refresh_token_and_rejects_another_identity(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(
            200, json={"AuthenticationResult": {"AccessToken": token()}}
        )

    c = client(tmp_path, handler)
    c.refresh()
    assert c.session["whoop-auth-refresh-token"] == "synthetic-refresh"
    assert json.loads(requests[0].content)["AuthFlow"] == "REFRESH_TOKEN_AUTH"
    assert c.path.stat().st_mode & 0o777 == 0o600
    before = c.path.read_bytes()
    c.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"AuthenticationResult": {"AccessToken": token(8)}}
            )
        )
    )
    with pytest.raises(DetailError, match="identity_mismatch"):
        c.refresh()
    assert c.path.read_bytes() == before


def test_transient_renewal_failure_keeps_session_and_redacts_body(tmp_path):
    c = client(tmp_path, lambda r: httpx.Response(503, text="private upstream content"))
    before = c.path.read_bytes()
    with pytest.raises(DetailError, match="^whoop_detail_refresh_unavailable$"):
        c.refresh()
    assert c.path.read_bytes() == before


def test_one_401_retry_then_success_and_expired_session_refreshes(tmp_path):
    paths = []

    def handler(request):
        paths.append(request.url.path)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "AuthenticationResult": {
                        "AccessToken": token(),
                        "RefreshToken": "rotated",
                    }
                },
            )
        if len(paths) == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"user": {"id": 7}})

    c = client(tmp_path, handler)
    c.verify()
    assert len(paths) == 3
    assert c.session["whoop-auth-refresh-token"] == "rotated"
    c.token = token(exp=1)
    c.verify()
    assert paths[-2:] == ["/auth-service/v3/whoop/", "/users-service/v2/bootstrap/"]


def test_hr_rejects_out_of_day_and_stress_wrong_day(tmp_path):
    start = datetime(2026, 9, 10, tzinfo=UTC)
    c = client(
        tmp_path,
        lambda r: httpx.Response(
            200,
            json={
                "name": "heart_rate",
                "values": [
                    {
                        "data": 60,
                        "time": (start - timedelta(seconds=1)).timestamp() * 1000,
                    }
                ],
            },
        ),
    )
    with pytest.raises(DetailError, match="invalid_heart_rate"):
        c.heart_rate(start, start + timedelta(days=1))
    c.http = httpx.Client(
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200,
                json={
                    "stress_graph": {},
                    "date_selector": {"previous_button_date": "2026-09-08"},
                },
            )
        )
    )
    with pytest.raises(DetailError, match="stress_date_mismatch"):
        c.stress(start.date())


def test_sync_uses_complete_moscow_day_and_keeps_sources_independent(
    clean_database, tmp_path
):
    with session_scope(clean_database) as session:
        session.add(
            WhoopConnection(
                profile_id=DEFAULT_PROFILE_ID,
                account_name="synthetic",
                external_user_id=7,
            )
        )
    c = client(tmp_path, lambda r: httpx.Response(200, json={"user": {"id": 7}}))
    windows = []

    def hr(start, end):
        windows.append((start, end))
        return {"name": "heart_rate", "values": []}

    c.heart_rate = hr
    c.stress = lambda day: (_ for _ in ()).throw(
        DetailError("whoop_detail_api_unavailable")
    )
    now = datetime(2026, 9, 11, 8, tzinfo=UTC)
    result = DetailService(PilotStore(clean_database), tmp_path).sync(c, now)
    assert windows[0] == (
        datetime(2026, 9, 9, 21, tzinfo=UTC),
        datetime(2026, 9, 10, 21, tzinfo=UTC),
    )
    assert windows[-1][1] == now
    assert result["status"] == "partial"
    assert result["resources"] == 2
    assert len(result["errors"]) == 2


def test_independent_hourly_schedules(tmp_path):
    executable = tmp_path / "health-agent"
    executable.write_text("#!/bin/sh\n")
    executable.chmod(0o700)
    env = tmp_path / ".env"
    atomic_private_write(env, b"SYNTHETIC=true\n")
    paths = LaunchdPaths.resolve(
        automation_root=tmp_path / "state",
        executable=executable,
        environment_file=env,
        working_directory=tmp_path,
        home=tmp_path,
    )
    for manager, label, command in (
        (DetailLaunchdManager, DETAIL_LABEL, "whoop-detail"),
        (QualityLaunchdManager, QUALITY_LABEL, "research"),
    ):
        payload = plistlib.loads(
            manager(paths, platform="darwin", uid=501)._plist_bytes()
        )
        assert payload["Label"] == label
        assert payload["StartInterval"] == 3600
        assert payload["ProgramArguments"][1] == command
        if command == "research":
            assert "PATH" in payload["EnvironmentVariables"]
            assert payload["StartCalendarInterval"] == {"Hour": 10, "Minute": 0}
