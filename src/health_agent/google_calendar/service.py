"""Idempotent profile-bound Calendar event synchronization."""

from __future__ import annotations

import hashlib
import html
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from health_agent.google_calendar.api import CalendarAPIError
from health_agent.google_calendar.models import CalendarEvent, CalendarResult
from health_agent.google_calendar.oauth import CalendarOAuthError


def event_id(profile_id: UUID, visit_id: UUID) -> str:
    return hashlib.sha256(
        f"health-agent-visit-v1:{profile_id}:{visit_id}".encode()
    ).hexdigest()


def _body(event: CalendarEvent) -> dict[str, Any]:
    private = {
        "profile_id": str(event.profile_id),
        "visit_id": str(event.visit_id),
        "managed_by": "health-agent-visit-v1",
    }
    questions = "\n".join(f"• {html.escape(q)}" for q in event.questions)
    return {
        "id": event_id(event.profile_id, event.visit_id),
        "summary": event.title,
        "description": questions,
        "start": {
            "dateTime": event.starts_at.isoformat(),
            "timeZone": event.timezone_name,
        },
        "end": {"dateTime": event.ends_at.isoformat(), "timeZone": event.timezone_name},
        "visibility": "private",
        "extendedProperties": {"private": private},
    }


def _remote_instant(value: object, timezone_name: str) -> datetime:
    if not isinstance(value, dict) or value.get("timeZone") != timezone_name:
        raise ValueError("invalid_remote_calendar_time")
    raw = value.get("dateTime")
    if not isinstance(raw, str) or "date" in value:
        raise ValueError("invalid_remote_calendar_time")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        raise ValueError("invalid_remote_calendar_time") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("invalid_remote_calendar_time")
    return parsed.astimezone(UTC)


def _managed_equal(
    remote: dict[str, Any], desired: dict[str, Any], timezone_name: str
) -> bool:
    if any(
        remote.get(key) != desired[key]
        for key in ("summary", "description", "visibility")
    ):
        return False
    return _remote_instant(remote.get("start"), timezone_name) == _remote_instant(
        desired["start"], timezone_name
    ) and _remote_instant(remote.get("end"), timezone_name) == _remote_instant(
        desired["end"], timezone_name
    )


class CalendarService:
    def __init__(self, profiles, tokens, oauth, gateway_factory):
        self.profiles, self.tokens, self.oauth, self.gateway_factory = (
            profiles,
            tokens,
            oauth,
            gateway_factory,
        )

    def sync(self, event: CalendarEvent) -> CalendarResult:
        eid = event_id(event.profile_id, event.visit_id)
        gateway = None
        try:
            profile = self.profiles.load(event.profile_id)
            token = self.tokens.load_verified(event.profile_id)
            if not profile.enabled or token is None:
                return CalendarResult(
                    eid, "deferred", safe_error="authorization_missing"
                )
            if (
                profile.account_subject != token["account_subject"]
                or profile.account_email.casefold() != token["account_email"]
            ):
                return CalendarResult(eid, "deferred", safe_error="account_mismatch")
            if self.oauth is None:
                return CalendarResult(
                    eid, "deferred", safe_error="authorization_missing"
                )
            credentials = self.oauth.stage(event.profile_id, interactive=False)
            gateway = self.gateway_factory(credentials)
            remote = gateway.get(profile.encoded_calendar_id, eid)
            recovered = False
            if remote is None:
                if event.cancelled:
                    return CalendarResult(eid, "unchanged")
                try:
                    remote = gateway.insert(profile.encoded_calendar_id, _body(event))
                except CalendarAPIError as error:
                    if error.status not in {0, 409}:
                        raise
                    remote = gateway.get(profile.encoded_calendar_id, eid)
                    if remote is None:
                        return CalendarResult(
                            eid, "deferred", safe_error="write_outcome_unknown"
                        )
                    recovered = True
                else:
                    recovered = False
                if not recovered:
                    return CalendarResult(eid, "created", remote.get("htmlLink"))
            private = remote.get("extendedProperties", {}).get("private", {})
            if (
                remote.get("id") != eid
                or not isinstance(remote.get("etag"), str)
                or private.get("profile_id") != str(event.profile_id)
                or private.get("visit_id") != str(event.visit_id)
                or private.get("managed_by") != "health-agent-visit-v1"
                or remote.get("attendees")
            ):
                return CalendarResult(
                    eid, "deferred", safe_error="remote_ownership_mismatch"
                )
            if event.cancelled:
                if remote.get("status") == "cancelled":
                    return CalendarResult(eid, "unchanged")
                gateway.patch(
                    profile.encoded_calendar_id,
                    eid,
                    {"status": "cancelled"},
                    remote.get("etag"),
                )
                return CalendarResult(eid, "cancelled")
            desired = _body(event)
            if _managed_equal(remote, desired, event.timezone_name):
                return CalendarResult(eid, "unchanged", remote.get("htmlLink"))
            updated = gateway.patch(
                profile.encoded_calendar_id,
                eid,
                {k: v for k, v in desired.items() if k != "id"},
                remote.get("etag"),
            )
            return CalendarResult(eid, "updated", updated.get("htmlLink"))
        except CalendarAPIError as error:
            return CalendarResult(eid, "deferred", safe_error=error.safe_code)
        except CalendarOAuthError as error:
            return CalendarResult(eid, "deferred", safe_error=str(error))
        except (TimeoutError, ConnectionError):
            return CalendarResult(eid, "deferred", safe_error="write_outcome_unknown")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError, AttributeError):
            return CalendarResult(
                eid, "deferred", safe_error="calendar_configuration_invalid"
            )
        finally:
            if gateway is not None:
                close = getattr(gateway, "close", None)
                if close is not None:
                    close()
