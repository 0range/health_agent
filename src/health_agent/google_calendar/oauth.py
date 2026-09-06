"""Exact-scope installed-app OAuth with verified OpenID identity."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore[import-untyped]

from health_agent.automation.storage import reject_symlink_components

SCOPES = frozenset(
    {
        "openid",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/calendar.events.owned",
    }
)
EMAIL_SCOPE_ALIASES = frozenset(
    {"email", "https://www.googleapis.com/auth/userinfo.email"}
)


class CalendarOAuthError(RuntimeError):
    pass


class CalendarOAuth:
    def __init__(self, client_secrets: Path, profiles, tokens, gateway_factory):
        self.client_secrets, self.profiles, self.tokens, self.gateway_factory = (
            Path(client_secrets),
            profiles,
            tokens,
            gateway_factory,
        )

    @staticmethod
    def _scopes(credentials: Credentials) -> set[str]:
        actual = set(credentials.granted_scopes or credentials.scopes or ())
        if "email" in actual:
            actual.remove("email")
            actual.add("https://www.googleapis.com/auth/userinfo.email")
        if actual != set(SCOPES):
            raise CalendarOAuthError("invalid_oauth_scopes")
        return actual

    def load(self, profile_id):
        value = self.tokens.load_verified(profile_id)
        if value is None:
            return None
        stored_scopes = value["credentials"].get("scopes")
        if not isinstance(stored_scopes, list):
            raise CalendarOAuthError("invalid_oauth_scopes")
        normalized = set(stored_scopes)
        if "email" in normalized:
            normalized.remove("email")
            normalized.add("https://www.googleapis.com/auth/userinfo.email")
        if normalized != set(SCOPES):
            raise CalendarOAuthError("invalid_oauth_scopes")
        credentials = Credentials.from_authorized_user_info(
            value["credentials"], sorted(SCOPES)
        )
        self._scopes(credentials)
        return credentials

    def stage(self, profile_id, interactive=False, force=False):
        self.profiles.load(profile_id)
        credentials = None if force else self.load(profile_id)
        if credentials is None:
            if not interactive:
                raise CalendarOAuthError("oauth_required")
            try:
                reject_symlink_components(self.client_secrets)
            except RuntimeError as error:
                raise CalendarOAuthError("client_secrets_invalid") from error
            if self.client_secrets.is_symlink() or not self.client_secrets.is_file():
                raise CalendarOAuthError("client_secrets_invalid")
            self.client_secrets.chmod(0o600)
            credentials = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets), sorted(SCOPES)
            ).run_local_server(
                host="127.0.0.1",
                port=0,
                timeout_seconds=300,
                access_type="offline",
                prompt="consent",
                include_granted_scopes="false",
            )
        elif credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(_BoundedRequest())
            except RefreshError as error:
                raise CalendarOAuthError("reauth_required") from error
            verified = self.tokens.load_verified(profile_id)
            if verified is None:
                raise CalendarOAuthError("oauth_required")
            self.tokens.publish_verified(
                profile_id,
                verified["account_subject"],
                verified["account_email"],
                json.loads(credentials.to_json()),
            )
        if not credentials.valid:
            raise CalendarOAuthError("reauth_required")
        self._scopes(credentials)
        return credentials

    def authorize(self, profile_id, interactive=False, force=False):
        credentials = self.stage(profile_id, interactive=interactive, force=force)
        identity = self.gateway_factory(credentials).userinfo(
            "https://openidconnect.googleapis.com/v1/userinfo"
        )
        subject, email = identity.get("sub"), identity.get("email")
        if (
            not isinstance(subject, str)
            or not isinstance(email, str)
            or not identity.get("email_verified")
        ):
            raise CalendarOAuthError("identity_verification_failed")
        profile = self.profiles.load(profile_id)
        if profile.account_subject not in (None, subject):
            raise CalendarOAuthError("account_mismatch")
        self.tokens.publish_verified(
            profile_id, subject, email, json.loads(credentials.to_json())
        )
        self.profiles.save(
            type(profile)(
                profile.profile_id,
                profile.calendar_id,
                subject,
                email.casefold(),
                profile.enabled,
            )
        )

    def local_status(self, profile_id):
        path = self.tokens.path_for(profile_id)
        if not path.exists() and not path.is_symlink():
            return "missing"
        try:
            info = path.lstat()
        except OSError:
            return "reauth_required"
        return (
            "ready"
            if info.st_mode & 0o777 == 0o600 and not path.is_symlink()
            else "reauth_required"
        )


class _BoundedRequest:
    def __init__(self):
        self.request = Request()

    def __call__(self, *args, **kwargs):
        kwargs["timeout"] = 30
        return self.request(*args, **kwargs)
