import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from google.oauth2.credentials import Credentials

from health_agent.google_calendar.api import CalendarAPIError
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


def test_authorize_verifies_boolean_identity_and_bound_subject(
    tmp_path: Path, monkeypatch
):
    profile_id = uuid4()
    service, tokens = oauth(tmp_path, profile_id)
    credential = credentials()
    monkeypatch.setattr(service, "stage", lambda *_args, **_kwargs: credential)

    class Identity:
        def __init__(self):
            self.payload = {
                "sub": "subject",
                "email": "Me@Example.test",
                "email_verified": True,
            }

        def userinfo(self, url):
            assert url == "https://openidconnect.googleapis.com/v1/userinfo"
            return self.payload

    identity = Identity()
    service.gateway_factory = lambda _: identity
    service.authorize(profile_id)
    assert tokens.load_verified(profile_id)["account_subject"] == "subject"
    assert service.profiles.load(profile_id).account_email == "me@example.test"

    other = uuid4()
    service.profiles.save(CalendarProfile(other))
    identity.payload = {"sub": "other", "email": "x@y.test", "email_verified": "false"}
    with pytest.raises(CalendarOAuthError, match="identity_verification_failed"):
        service.authorize(other)
    assert tokens.load_verified(other) is None

    bound = uuid4()
    service.profiles.save(
        CalendarProfile(bound, account_subject="expected", account_email="e@x.test")
    )
    identity.payload = {"sub": "different", "email": "x@y.test", "email_verified": True}
    with pytest.raises(CalendarOAuthError, match="account_mismatch"):
        service.authorize(bound)


def test_expired_refresh_is_bounded_and_persisted(tmp_path: Path, monkeypatch):
    profile_id = uuid4()
    service, tokens = oauth(tmp_path, profile_id)
    expired = credentials()
    expired.expiry = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    tokens.publish_verified(
        profile_id, "subject", "a@b.test", json.loads(expired.to_json())
    )

    def refresh(candidate, request):
        assert request.timeout_seconds == 30
        candidate.token = "refreshed"
        candidate.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)

    monkeypatch.setattr(Credentials, "refresh", refresh)
    assert service.stage(profile_id, interactive=False).token == "refreshed"
    assert tokens.load_verified(profile_id)["credentials"]["token"] == "refreshed"


def test_fully_mocked_interactive_flow_verifies_and_publishes(
    tmp_path: Path, monkeypatch
):
    profile_id = uuid4()
    service, tokens = oauth(tmp_path, profile_id)
    service.client_secrets = tmp_path / "client.json"
    service.client_secrets.write_text("{}")
    calls = {}

    class Flow:
        def run_local_server(self, **kwargs):
            calls["server"] = kwargs
            return credentials()

    def from_file(path, scopes):
        calls["path"], calls["scopes"] = path, scopes
        return Flow()

    class Identity:
        closed = False

        def userinfo(self, url):
            calls["userinfo"] = url
            return {
                "sub": "interactive-sub",
                "email": "Me@Example.test",
                "email_verified": True,
            }

        def close(self):
            self.closed = True

    monkeypatch.setattr(
        "health_agent.google_calendar.oauth.InstalledAppFlow.from_client_secrets_file",
        from_file,
    )
    identity = Identity()
    service.gateway_factory = lambda _: identity
    service.authorize(profile_id, interactive=True, force=True)

    assert calls["scopes"] == sorted(SCOPES)
    assert calls["server"] == {
        "host": "127.0.0.1",
        "port": 0,
        "timeout_seconds": 300,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    assert calls["userinfo"] == "https://openidconnect.googleapis.com/v1/userinfo"
    assert tokens.load_verified(profile_id)["account_subject"] == "interactive-sub"
    assert identity.closed


def test_userinfo_gateway_closes_on_error(tmp_path: Path, monkeypatch):
    profile_id = uuid4()
    service, tokens = oauth(tmp_path, profile_id)
    monkeypatch.setattr(service, "stage", lambda *_args, **_kwargs: credentials())

    class BrokenIdentity:
        closed = False

        def userinfo(self, _url):
            raise CalendarAPIError(500)

        def close(self):
            self.closed = True

    gateway = BrokenIdentity()
    service.gateway_factory = lambda _: gateway
    with pytest.raises(CalendarAPIError, match="google_unavailable"):
        service.authorize(profile_id)
    assert gateway.closed
    assert tokens.load_verified(profile_id) is None
