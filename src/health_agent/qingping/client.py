"""Official read-only Qingping API calls with short-lived client credentials."""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import httpx

from health_agent.automation.storage import atomic_private_write, require_private_file


class QingpingError(RuntimeError):
    """Only fixed, content-free error codes may cross the CLI boundary."""


class QingpingClient:
    def __init__(
        self,
        http: httpx.Client,
        app_key: str,
        app_secret: str,
        token_path: Path | None = None,
    ) -> None:
        self.http = http
        self.credentials = (app_key, app_secret)
        self.token = ""
        self.expires_at = 0.0
        self.last_timestamp = 0
        self.token_path = token_path
        self.identity = hashlib.sha256(
            (app_key + ":" + app_secret).encode()
        ).hexdigest()
        if token_path is not None and token_path.exists():
            cached = json.loads(require_private_file(token_path.absolute()).read_text())
            if cached.get("identity") == self.identity and isinstance(
                cached.get("access_token"), str
            ):
                remaining = float(cached.get("expires_at", 0)) - time.time() - 60
                if remaining > 0:
                    self.token = cached["access_token"]
                    self.expires_at = time.monotonic() + remaining

    def _authorize(self) -> None:
        try:
            response = self.http.post(
                "https://oauth.cleargrass.com/oauth2/token",
                auth=self.credentials,
                data={
                    "grant_type": "client_credentials",
                    "scope": "device_full_access",
                },
            )
            if response.status_code != 200:
                raise QingpingError("qingping_token_unavailable")
            payload = response.json()
            token, expires = payload["access_token"], int(payload["expires_in"])
            if not isinstance(token, str) or not token or expires <= 60:
                raise ValueError
            self.token = token
            self.expires_at = time.monotonic() + expires - 60
            if self.token_path is not None:
                atomic_private_write(
                    self.token_path,
                    json.dumps(
                        {
                            "identity": self.identity,
                            "access_token": token,
                            "expires_at": time.time() + expires,
                        }
                    ).encode(),
                )
        except (httpx.HTTPError, KeyError, ValueError, TypeError):
            raise QingpingError("qingping_token_unavailable") from None

    def _get(self, path: str, parameters: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(2):
            if not self.token or time.monotonic() >= self.expires_at:
                self._authorize()
            self.last_timestamp = max(
                time.time_ns() // 1_000_000, self.last_timestamp + 1
            )
            try:
                response = self.http.get(
                    "https://apis.cleargrass.com/v1/apis/" + path,
                    params={**parameters, "timestamp": self.last_timestamp},
                    headers={"Authorization": "Bearer " + self.token},
                )
                if response.status_code == 401 and attempt == 0:
                    self.token = ""
                    continue
                if response.status_code != 200:
                    raise QingpingError("qingping_api_unavailable")
                payload = response.json()
                if not isinstance(payload, dict):
                    raise TypeError
                return payload
            except (httpx.HTTPError, ValueError, TypeError):
                raise QingpingError("qingping_api_unavailable") from None
        raise QingpingError("qingping_api_unavailable")

    def _pages(
        self, path: str, key: str, parameters: dict[str, Any], limit: int
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for _ in range(1000):
            payload = self._get(
                path, {**parameters, "limit": limit, "offset": len(result)}
            )
            rows, total = payload.get(key), payload.get("total")
            if (
                not isinstance(rows, list)
                or not isinstance(total, int)
                or isinstance(total, bool)
                or total < 0
                or any(not isinstance(row, dict) for row in rows)
            ):
                raise QingpingError("qingping_invalid_page")
            result.extend(rows)
            if len(result) >= total:
                return result
            if not rows:
                raise QingpingError("qingping_incomplete_history")
        raise QingpingError("qingping_page_limit")

    def devices(self) -> list[dict[str, Any]]:
        return self._pages("devices", "devices", {}, 50)

    def history(self, mac: str, start: int, end: int) -> list[dict[str, Any]]:
        return self._pages(
            "devices/data",
            "data",
            {"mac": mac, "start_time": start, "end_time": end},
            200,
        )
