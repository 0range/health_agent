import json
from pathlib import Path
from uuid import uuid4

import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_calendar.models import CalendarProfile
from health_agent.google_calendar.oauth import SCOPES, CalendarOAuth, CalendarOAuthError
from health_agent.google_calendar.stores import CalendarProfileStore, CalendarTokenStore


def credentials(scopes=None):
    return Credentials(
        token="access",
        refresh_token="refresh",
        token_uri="https://oauth2.googleapis.com/token",
        client_id="client",
        client_secret="secret",
        scopes=scopes or sorted(SCOPES),
    )


def oauth(tmp_path: Path, profile_id):
    profiles = CalendarProfileStore(tmp_path / "profiles")
    tokens = CalendarTokenStore(tmp_path / "tokens")
    profiles.save(CalendarProfile(profile_id))
    return CalendarOAuth(Path("missing"), profiles, tokens, lambda _: None), tokens


def test_missing_authorization_is_local_and_noninteractive(tmp_path: Path):
    profile_id = uuid4()
    service, _ = oauth(tmp_path, profile_id)
    with pytest.raises(CalendarOAuthError, match="oauth_required"):
        service.stage(profile_id, interactive=False)
    assert service.local_status(profile_id) == "missing"


def test_exact_scopes_accept_email_alias_and_reject_wider(tmp_path: Path):
    profile_id = uuid4()
    service, tokens = oauth(tmp_path, profile_id)
    alias_scopes = (
        set(SCOPES) - {"https://www.googleapis.com/auth/userinfo.email"}
    ) | {"email"}
    token = credentials(sorted(alias_scopes))
    tokens.publish_verified(
        profile_id, "subject", "a@b.test", json.loads(token.to_json())
    )
    assert service.load(profile_id) is not None

    other = uuid4()
    service.profiles.save(CalendarProfile(other))
    wider = credentials([*SCOPES, "https://www.googleapis.com/auth/calendar"])
    tokens.publish_verified(other, "other", "c@d.test", json.loads(wider.to_json()))
    with pytest.raises(CalendarOAuthError, match="invalid_oauth_scopes"):
        service.load(other)
