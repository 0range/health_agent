"""Original physiological series from the account owner's WHOOP app session.

These interfaces are separate from the public developer API. Keep raw responses
and their actual resolution; UI graph coordinates are not UTC sample times.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx
from sqlalchemy import select

from health_agent.automation.storage import atomic_private_write, require_private_file
from health_agent.db import session_scope
from health_agent.pilot.storage import PilotStore
from health_agent.research.calendar import bounds, recent_days
from health_agent.whoop.models import WhoopConnection, WhoopSleep


class DetailError(RuntimeError):
    """Fixed error codes only; upstream bodies can contain credentials or health data."""


def claims(token: str) -> dict[str, Any]:
    try:
        value = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        if not isinstance(value, dict):
            raise TypeError
        return value
    except (ValueError, TypeError, IndexError):
        raise DetailError("whoop_detail_invalid_session") from None


class DetailClient:
    def __init__(self, http: httpx.Client, session_file: Path) -> None:
        self.http, self.path = http, session_file
        self.session = json.loads(
            require_private_file(session_file.absolute()).read_text()
        )
        self.profile_id = UUID(self.session["profile_id"])
        self.user_id = int(self.session["user_id"])
        self.token = unquote(self.session["whoop-auth-token"])
        self._validate_identity(self.token)

    def _validate_identity(self, token: str) -> None:
        if int(claims(token).get("custom:user_id", -1)) != self.user_id:
            raise DetailError("whoop_detail_identity_mismatch")

    def refresh(self) -> None:
        try:
            response = self.http.post(
                "https://api.prod.whoop.com/auth-service/v3/whoop/",
                headers={
                    **{
                        key: value
                        for key, value in self.session.get(
                            "refresh_headers", {}
                        ).items()
                        if key
                        in {
                            "accept",
                            "x-amz-user-agent",
                            "origin",
                            "referer",
                            "amz-sdk-request",
                            "user-agent",
                        }
                    },
                    "content-type": "application/x-amz-json-1.1",
                    "x-amz-target": "AWSCognitoIdentityProviderService.InitiateAuth",
                },
                json={
                    "AuthFlow": "REFRESH_TOKEN_AUTH",
                    "ClientId": claims(self.token)["client_id"],
                    "AuthParameters": {
                        "REFRESH_TOKEN": unquote(
                            self.session["whoop-auth-refresh-token"]
                        )
                    },
                },
            )
            if response.status_code != 200:
                raise DetailError("whoop_detail_refresh_unavailable")
            auth = response.json()["AuthenticationResult"]
            token = auth["AccessToken"]
            self._validate_identity(token)
            updated = {
                **self.session,
                "whoop-auth-token": token,
                "whoop-auth-refresh-token": auth.get(
                    "RefreshToken", self.session["whoop-auth-refresh-token"]
                ),
            }
            atomic_private_write(self.path, json.dumps(updated).encode())
            self.session, self.token = updated, token
        except (httpx.HTTPError, KeyError, TypeError, ValueError):
            raise DetailError("whoop_detail_refresh_unavailable") from None

    def get(self, path: str, parameters: dict[str, Any] | None = None) -> Any:
        for attempt in range(2):
            if float(claims(self.token).get("exp", 0)) <= time.time() + 300:
                self.refresh()
            try:
                response = self.http.get(
                    "https://api.prod.whoop.com" + path,
                    params=parameters,
                    headers={"Authorization": "Bearer " + self.token},
                )
                if response.status_code == 401 and attempt == 0:
                    self.refresh()
                    continue
                if response.status_code != 200:
                    raise DetailError("whoop_detail_api_unavailable")
                return response.json()
            except (httpx.HTTPError, ValueError):
                raise DetailError("whoop_detail_api_unavailable") from None
        raise DetailError("whoop_detail_api_unavailable")

    def verify(self) -> None:
        raw = self.get(
            "/users-service/v2/bootstrap/",
            {"apiVersion": 7, "accountType": "users", "id": 0},
        )
        if (
            not isinstance(raw, dict)
            or int(raw.get("user", {}).get("id", -1)) != self.user_id
        ):
            raise DetailError("whoop_detail_identity_mismatch")

    def heart_rate(self, start: datetime, end: datetime) -> dict[str, Any]:
        raw = self.get(
            f"/metrics-service/v1/metrics/user/{self.user_id}",
            {
                "name": "heart_rate",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "step": 6,
                "order": "t",
                "apiVersion": 7,
            },
        )
        if (
            not isinstance(raw, dict)
            or raw.get("name") != "heart_rate"
            or not isinstance(raw.get("values"), list)
        ):
            raise DetailError("whoop_detail_invalid_heart_rate")
        for sample in raw["values"]:
            timestamp = sample.get("time") if isinstance(sample, dict) else None
            if (
                not isinstance(timestamp, (int, float))
                or isinstance(timestamp, bool)
                or not start.timestamp() * 1000 <= timestamp <= end.timestamp() * 1000
            ):
                raise DetailError("whoop_detail_invalid_heart_rate")
        return raw

    def stress(self, day: date) -> dict[str, Any]:
        raw = self.get(f"/health-service/v2/stress-bff/{day.isoformat()}")
        if not isinstance(raw, dict) or not isinstance(raw.get("stress_graph"), dict):
            raise DetailError("whoop_detail_invalid_stress")
        if (
            raw.get("date_selector", {}).get("previous_button_date")
            != (day - timedelta(days=1)).isoformat()
        ):
            raise DetailError("whoop_detail_stress_date_mismatch")
        return raw


class DetailService:
    def __init__(self, store: PilotStore, root: Path) -> None:
        self.store, self.root = store, root

    def archive(
        self,
        client: DetailClient,
        family: str,
        identity: str,
        raw: Any,
        at: datetime,
        captured: datetime,
    ) -> None:
        content = json.dumps(
            raw, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        digest = hashlib.sha256(content.encode()).hexdigest()
        record = self.store.put(
            client.profile_id,
            "shared",
            "whoop_detail",
            f"whoop-app:{client.user_id}:Europe-Moscow:{family}:{identity}:{digest}",
            {
                "provider": "whoop_app",
                "resource": family,
                "logical_id": identity,
                "external_user_id": client.user_id,
                "captured_at": captured.isoformat(),
                "content_hash": digest,
                "raw": raw,
                "calendar_timezone": "Europe/Moscow",
                "resolution": "requested_6_seconds"
                if family == "heart_rate"
                else "native_source",
            },
            at=at,
        )
        # A -> B -> A is a real revision sequence: first retrieval time alone
        # would incorrectly select B. Keep raw payload immutable and separately
        # update the last time this exact content was observed.
        self.store.patch(
            client.profile_id,
            record.id,
            {
                **record.payload,
                "last_seen_at": captured.isoformat(),
            },
        )

    def sync(self, client: DetailClient, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        with session_scope(self.store.engine) as session:
            connection = session.scalar(
                select(WhoopConnection).where(
                    WhoopConnection.profile_id == client.profile_id,
                    WhoopConnection.external_user_id == client.user_id,
                )
            )
            if connection is None:
                raise DetailError("whoop_detail_unconfigured_profile")
            sleeps = [
                (s.external_id, s.start_at)
                for s in session.scalars(
                    select(WhoopSleep).where(
                        WhoopSleep.profile_id == client.profile_id,
                        WhoopSleep.connection_id == connection.id,
                        WhoopSleep.end_at
                        >= bounds(recent_days(now, include_today=True)[0])[0],
                        WhoopSleep.end_at <= now,
                    )
                )
            ]
        client.verify()
        errors: list[str] = []
        saved = 0
        # Daily snapshots preserve revisions and original values; no resampling.
        for day in recent_days(now, include_today=True):
            start, end = bounds(day)
            end = min(end, now)
            for family in ("heart_rate", "stress"):
                try:
                    raw = (
                        client.heart_rate(start, end)
                        if family == "heart_rate"
                        else client.stress(day)
                    )
                    self.archive(client, family, day.isoformat(), raw, start, now)
                    saved += 1
                except DetailError as error:
                    errors.append(f"{family}:{error}")
        for sleep_id, start in sleeps:
            try:
                raw = client.get(
                    "/sleep-service/v1/sleep-events",
                    {"activityId": sleep_id, "apiVersion": 7},
                )
                if not isinstance(raw, list) or any(
                    not isinstance(row, dict)
                    or "during" not in row
                    or "type" not in row
                    for row in raw
                ):
                    raise DetailError("whoop_detail_invalid_sleep_events")
                self.archive(client, "sleep_stages", sleep_id, raw, start, now)
                saved += 1
            except DetailError as error:
                errors.append(f"sleep_stages:{error}")
        result = {
            "status": "partial" if errors else "synced",
            "last_attempt": now.isoformat(),
            "resources": saved,
            "errors": errors,
            "profile_id": str(client.profile_id),
            "session_expires_at": datetime.fromtimestamp(
                claims(client.token)["exp"], UTC
            ).isoformat(),
        }
        atomic_private_write(self.root / "state.json", json.dumps(result).encode())
        return result
