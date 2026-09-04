from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx

from health_agent.whoop.oauth import (
    WHOOP_SCOPES,
    WhoopOAuth,
    WhoopOAuthError,
    WhoopOAuthScopesError,
)
from health_agent.whoop.tokens import TokenStore, TokenStoreError, WhoopToken

API_BASE_URL = "https://api.prod.whoop.com/developer"
PROFILE_PATH = "/v2/user/profile/basic"
BODY_PATH = "/v2/user/measurement/body"
CYCLE_PATH = "/v2/cycle"
RECOVERY_PATH = "/v2/recovery"
SLEEP_PATH = "/v2/activity/sleep"
WORKOUT_PATH = "/v2/activity/workout"
COLLECTION_PATHS = (CYCLE_PATH, RECOVERY_PATH, SLEEP_PATH, WORKOUT_PATH)


class WhoopApiError(RuntimeError):
    """Safe WHOOP API failure without response content or credentials."""


class WhoopAuthorizationRequired(WhoopApiError):
    """The account must repeat the human OAuth step."""


class WhoopRateLimitDeferred(WhoopApiError):
    """WHOOP asked the caller to resume after a future instant."""

    def __init__(self, retry_at: datetime) -> None:
        super().__init__("WHOOP synchronization is deferred by rate limiting")
        self.retry_at = retry_at


def _iso8601(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("WHOOP date bounds must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class WhoopClient:
    """Small client for the documented WHOOP Developer API v2 surface."""

    def __init__(
        self,
        oauth: WhoopOAuth,
        token_store: TokenStore,
        profile_slug: str,
        account_name: str,
        *,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
        max_attempts: int = 4,
        max_inline_wait_seconds: float = 60,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._oauth = oauth
        self._tokens = token_store
        self._profile_slug = profile_slug
        self._account_name = account_name
        self._http = http_client or httpx.Client(timeout=30)
        self._sleep = sleeper
        self._clock = clock or (lambda: datetime.now(UTC))
        self._max_attempts = max_attempts
        self._max_inline_wait_seconds = max_inline_wait_seconds

    @contextmanager
    def operation(self) -> Iterator[None]:
        """Take the global outer account lock before database or token locks."""
        try:
            with self._tokens.operation(self._profile_slug, self._account_name):
                yield
        except TokenStoreError as error:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization storage is unavailable"
            ) from error

    def recover_token(self, committed_generation: UUID | None) -> None:
        try:
            self._tokens.recover(
                self._profile_slug, self._account_name, committed_generation
            )
        except TokenStoreError as error:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization must be renewed"
            ) from error

    def get_object(self, path: str) -> dict[str, Any]:
        payload = self._request(path, {})
        if not isinstance(payload, dict):
            raise WhoopApiError("WHOOP API returned an invalid object")
        return payload

    def iter_collection(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        for page in self.iter_collection_pages(path, start=start, end=end):
            yield from page

    def iter_collection_pages(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[tuple[dict[str, Any], ...]]:
        if path not in COLLECTION_PATHS:
            raise ValueError("Unsupported WHOOP collection path")
        params: dict[str, str | int] = {"limit": 25}
        if start is not None:
            params["start"] = _iso8601(start)
        if end is not None:
            params["end"] = _iso8601(end)
        seen_tokens: set[str] = set()
        while True:
            payload = self._request(path, params)
            if not isinstance(payload, dict) or not isinstance(
                payload.get("records"), list
            ):
                raise WhoopApiError("WHOOP API returned an invalid collection")
            records = payload["records"]
            if not all(isinstance(record, dict) for record in records):
                raise WhoopApiError("WHOOP API returned an invalid collection")
            yield tuple(records)
            raw_next_token = payload.get("next_token")
            if raw_next_token is None or raw_next_token == "":
                return
            if not isinstance(raw_next_token, str):
                raise WhoopApiError("WHOOP API returned an invalid pagination token")
            if raw_next_token in seen_tokens:
                raise WhoopApiError("WHOOP API repeated a pagination token")
            seen_tokens.add(raw_next_token)
            params["nextToken"] = raw_next_token

    def _load_token(self) -> WhoopToken:
        try:
            token = self._tokens.load(self._profile_slug, self._account_name)
        except TokenStoreError as error:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization must be renewed"
            ) from error
        if token is None:
            raise WhoopAuthorizationRequired("WHOOP account is not authorized")
        if token.expired:
            return self._refresh_token(token.refresh_token)
        return self._validate_scopes(token)

    def _refresh_token(self, stale_refresh_token: str) -> WhoopToken:
        def refresh_with_required_scopes(refresh_token: str) -> WhoopToken:
            return self._validate_scopes(self._oauth.refresh(refresh_token))

        try:
            return self._validate_scopes(
                self._tokens.rotate(
                    self._profile_slug,
                    self._account_name,
                    stale_refresh_token,
                    refresh_with_required_scopes,
                )
            )
        except WhoopOAuthScopesError as error:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization is missing required scopes"
            ) from error
        except (TokenStoreError, WhoopOAuthError) as error:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization must be renewed"
            ) from error

    @staticmethod
    def _validate_scopes(token: WhoopToken) -> WhoopToken:
        if set(WHOOP_SCOPES).difference(token.scopes):
            raise WhoopAuthorizationRequired(
                "WHOOP authorization is missing required scopes"
            )
        return token

    def _request(self, path: str, params: dict[str, str | int]) -> Any:
        token = self._load_token()
        refreshed_after_unauthorized = False
        last_status: int | None = None
        transient_attempt = 0
        while transient_attempt < self._max_attempts:
            try:
                response = self._http.get(
                    f"{API_BASE_URL}{path}",
                    params=params,
                    headers={"Authorization": f"Bearer {token.access_token}"},
                )
            except httpx.TransportError as error:
                transient_attempt += 1
                if transient_attempt == self._max_attempts:
                    raise WhoopApiError(
                        "WHOOP API is temporarily unavailable"
                    ) from error
                self._sleep(float(2 ** (transient_attempt - 1)))
                continue

            last_status = response.status_code
            if response.status_code == 401 and not refreshed_after_unauthorized:
                token = self._refresh_token(token.refresh_token)
                refreshed_after_unauthorized = True
                continue
            if response.status_code == 429 or response.status_code >= 500:
                delay = self._retry_delay(response, transient_attempt)
                if (
                    response.status_code == 429
                    and delay > self._max_inline_wait_seconds
                ):
                    raise WhoopRateLimitDeferred(
                        self._clock() + timedelta(seconds=delay)
                    )
                transient_attempt += 1
                if transient_attempt == self._max_attempts:
                    break
                self._sleep(delay)
                continue
            if not 200 <= response.status_code < 300:
                if response.status_code == 401:
                    raise WhoopAuthorizationRequired(
                        "WHOOP authorization must be renewed"
                    )
                raise WhoopApiError(f"WHOOP API returned status {response.status_code}")
            try:
                return response.json()
            except ValueError as error:
                raise WhoopApiError("WHOOP API returned invalid JSON") from error
        raise WhoopApiError(f"WHOOP API remained unavailable (status {last_status})")

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        for header in ("X-RateLimit-Reset", "Retry-After"):
            raw_value = response.headers.get(header)
            if raw_value is not None:
                try:
                    return max(0.0, float(raw_value))
                except ValueError:
                    pass
        return float(2**attempt)
