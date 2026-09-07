"""Durable, replay-safe ingestion of COROS activity history."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from health_agent.automation.storage import atomic_private_write
from health_agent.pilot.contracts import Store
from health_agent.pilot.coros import (
    CorosHTTPTransport,
    CorosMcpTransport,
    CorosReadClient,
)
from health_agent.pilot.coros_auth import CorosOAuth

COROS_RESULT_LIMIT = 100
MOSCOW = ZoneInfo("Europe/Moscow")


class _ArchivingTransport:
    """Archive each tool result before the adapter is allowed to parse it."""

    def __init__(
        self,
        transport: CorosMcpTransport,
        root: Path,
        clock: Callable[[], datetime],
    ) -> None:
        self.transport = transport
        self.root = root
        self.clock = clock
        self.calls = 0

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        payload = self.transport.call_tool(name, arguments)
        fetched_at = self.clock().astimezone(UTC)
        archive = {
            "tool": name,
            "requested_range": {
                "since": arguments.get("startDate"),
                "until": arguments.get("endDate"),
            },
            "fetched_at": fetched_at.isoformat(),
            "payload": _without_tokens(payload),
        }
        stamp = fetched_at.strftime("%Y%m%dT%H%M%S.%fZ")
        path = self.root / "raw" / f"{stamp}-{uuid4().hex}.json"
        atomic_private_write(
            path,
            (json.dumps(archive, ensure_ascii=False, sort_keys=True) + "\n").encode(),
        )
        self.calls += 1
        return payload


class CorosSync:
    """Download original COROS responses and append normalized activities."""

    def __init__(
        self,
        store: Store,
        auth: CorosOAuth,
        profile_id: UUID,
        root: Path,
        *,
        transport: CorosMcpTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.store = store
        self.auth = auth
        self.profile_id = profile_id
        self.root = root
        self.clock = clock or (lambda: datetime.now(UTC))
        archive = _ArchivingTransport(
            transport or CorosHTTPTransport(auth), root, self.clock
        )
        self._archive = archive
        self.client = CorosReadClient(archive)

    def sync(self, since: date, until: date) -> dict[str, int]:
        if until < since:
            raise ValueError("until must not be before since")
        started_at = self.clock().astimezone(UTC)
        calls_before = self._archive.calls
        counts = {"requests": 0, "fetched": 0, "confirmed": 0, "incomplete": 0}
        seen: set[str] = set()
        try:
            for window_start, window_end in _month_windows(since, until):
                activities, incomplete = self._fetch(window_start, window_end)
                counts["incomplete"] += incomplete
                for activity in activities:
                    identifier = _activity_id(activity)
                    if identifier in seen:
                        continue
                    seen.add(identifier)
                    counts["fetched"] += 1
                    payload, observed_at = _activity_record(activity)
                    record = self.store.put(
                        self.profile_id,
                        "training",
                        "activity",
                        f"activity:{identifier}",
                        payload,
                        at=observed_at,
                    )
                    if record.payload == payload:
                        counts["confirmed"] += 1
            counts["requests"] = self._archive.calls - calls_before
            self._record_run(
                started_at,
                since,
                until,
                "incomplete" if counts["incomplete"] else "success",
                counts,
            )
            return counts
        except Exception as error:
            counts["requests"] = self._archive.calls - calls_before
            self._record_run(
                started_at,
                since,
                until,
                "failed",
                counts,
                failure=type(error).__name__,
            )
            raise

    def _fetch(self, since: date, until: date) -> tuple[list[dict[str, Any]], int]:
        activities = self.client.activities(_midnight(since), _midnight(until))
        if len(activities) < COROS_RESULT_LIMIT:
            return activities, 0
        if since == until:
            return activities, 1
        midpoint = since + timedelta(days=(until - since).days // 2)
        left, left_incomplete = self._fetch(since, midpoint)
        right, right_incomplete = self._fetch(midpoint + timedelta(days=1), until)
        return left + right, left_incomplete + right_incomplete

    def _record_run(
        self,
        started_at: datetime,
        since: date,
        until: date,
        status: str,
        counts: dict[str, int],
        *,
        failure: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "status": status,
            "since": since.isoformat(),
            "until": until.isoformat(),
            "started_at": started_at.isoformat(),
            "finished_at": self.clock().astimezone(UTC).isoformat(),
            "counts": dict(counts),
        }
        if failure is not None:
            payload["failure"] = failure
        self.store.put(
            self.profile_id,
            "training",
            "sync_run",
            f"coros-sync:{started_at.isoformat()}:{uuid4().hex}",
            payload,
            at=started_at,
        )


def run_coros_sync(
    store: Store,
    auth: CorosOAuth,
    profile_id: UUID,
    root: Path,
    since: date,
    until: date,
    *,
    transport: CorosMcpTransport | None = None,
) -> dict[str, int]:
    """Composition-root-friendly one-shot interface."""
    return CorosSync(
        store, auth, profile_id, root, transport=transport
    ).sync(since, until)


def _month_windows(since: date, until: date):
    cursor = since
    while cursor <= until:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        end = min(until, next_month - timedelta(days=1))
        yield cursor, end
        cursor = end + timedelta(days=1)


def _midnight(value: date) -> datetime:
    return datetime.combine(value, time.min, tzinfo=UTC)


def _activity_id(activity: dict[str, Any]) -> str:
    identifier = activity.get("id", activity.get("activity_id"))
    if identifier is None or not str(identifier).strip():
        raise ValueError("COROS activity is missing an id")
    return str(identifier)


def _activity_record(activity: dict[str, Any]) -> tuple[dict[str, Any], datetime]:
    payload = dict(activity)
    value = activity.get("started_at")
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value)
    else:
        provider_date = activity.get("date")
        if not isinstance(provider_date, str):
            raise TypeError("COROS activity is missing its date")
        try:
            parsed = datetime.combine(date.fromisoformat(provider_date), time.min, MOSCOW)
        except ValueError as error:
            raise ValueError("COROS activity date is invalid") from error
        # This timestamp exists only to make date-indexed Store queries possible.
        # Consumers must not infer an activity start time from a day-precision row.
        payload["timestamp_precision"] = "day"
    if parsed.tzinfo is None:
        raise ValueError("COROS activity started_at must include a timezone")
    return payload, parsed


def _without_tokens(value: Any) -> Any:
    """Defence in depth: tool results should not contain OAuth credentials."""
    if isinstance(value, dict):
        return {
            key: "[redacted]" if "token" in key.lower() else _without_tokens(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_without_tokens(item) for item in value]
    return value
