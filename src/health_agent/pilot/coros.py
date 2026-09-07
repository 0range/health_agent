"""Small, read-only adapter for the official COROS MCP server.

OAuth and MCP session handling deliberately belong to the injected transport.  This
keeps access tokens out of the health-agent store and makes the boundary testable.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx

from health_agent.pilot.coros_auth import CorosAuthError, CorosOAuth

COROS_MCP_ENDPOINT = "https://mcp.coros.com/mcp"
COROS_ACTIVITY_TOOL = "querySportRecords"
_ACTIVITY_LIMIT = 100
COROS_READ_TOOLS = frozenset(
    {
        "querySportRecords",
        "getActivityDetail",
        "queryActivityLapData",
        "queryCustomActivityLapData",
        "queryDailyHealthData",
        "querySleepData",
        "querySleepHrv",
        "queryAvgHeartRate",
        "queryRestingHeartRate",
        "queryStressLevel",
        "queryHealthCheckTimeSeries",
        "queryStressTimeSeries",
        "queryRecoveryStatus",
        "queryMenstruationCycles",
        "queryFitnessAssessmentOverview",
        "queryTrainingLoadAssessment",
        "queryTrainingSchedule",
        "queryTrainingPlanDetail",
        "queryDevices",
        "queryUserInfo",
    }
)


class CorosMcpTransport(Protocol):
    """An authenticated MCP transport (for example an OAuth-aware MCP client)."""

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]: ...


class CorosHTTPTransport:
    """Authenticated streamable-HTTP MCP client with a strict read allowlist."""

    def __init__(
        self, auth: CorosOAuth, *, http_client: httpx.Client | None = None
    ) -> None:
        self._auth = auth
        self._http = http_client or httpx.Client(timeout=30, follow_redirects=True)
        self._session_id: str | None = None
        self._request_id = 0
        self._initialized = False

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]:
        if name not in COROS_READ_TOOLS:
            raise ValueError("COROS tool is not an allowlisted read tool")
        token_type, access_token, endpoint = self._auth.access()
        headers = {
            "Authorization": f"{token_type} {access_token}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": "2025-06-18",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        if not self._initialized:
            initialized = self._post(
                endpoint,
                headers,
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "health-agent", "version": "0.1.0"},
                },
            )
            if "error" in initialized:
                raise CorosAuthError("COROS MCP initialization failed")
            if self._session_id:
                headers["Mcp-Session-Id"] = self._session_id
            self._notify_initialized(endpoint, headers)
            self._initialized = True
        response = self._post(
            endpoint, headers, "tools/call", {"name": name, "arguments": arguments}
        )
        if "error" in response or not isinstance(response.get("result"), dict):
            raise CorosAuthError("COROS MCP tool call failed")
        return response["result"]  # type: ignore[return-value]

    def _notify_initialized(self, endpoint: str, headers: dict[str, str]) -> None:
        try:
            response = self._http.post(
                endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "method": "notifications/initialized",
                    "params": {},
                },
            )
        except httpx.HTTPError as error:
            raise CorosAuthError("COROS MCP endpoint is unavailable") from error
        if response.status_code not in (200, 202, 204):
            raise CorosAuthError(
                f"COROS MCP initialized notification returned status {response.status_code}"
            )

    def _post(
        self,
        endpoint: str,
        headers: dict[str, str],
        method: str,
        params: dict[str, object],
    ) -> dict[str, Any]:
        self._request_id += 1
        try:
            response = self._http.post(
                endpoint,
                headers=headers,
                json={
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": method,
                    "params": params,
                },
            )
        except httpx.HTTPError as error:
            raise CorosAuthError("COROS MCP endpoint is unavailable") from error
        if response.status_code != 200:
            raise CorosAuthError(
                f"COROS MCP endpoint returned status {response.status_code}"
            )
        session_id = response.headers.get("Mcp-Session-Id")
        if session_id:
            self._session_id = session_id
        payload = self._response_payload(response)
        if not isinstance(payload, dict):
            raise CorosAuthError("COROS MCP returned an invalid response")
        return payload

    @staticmethod
    def _response_payload(response: httpx.Response) -> dict[str, Any]:
        if "text/event-stream" not in response.headers.get("content-type", ""):
            value = response.json()
            if not isinstance(value, dict):
                raise CorosAuthError("COROS MCP returned an invalid response")
            return value
        for event in response.text.split("\n\n"):
            data = "\n".join(
                line[5:].lstrip()
                for line in event.splitlines()
                if line.startswith("data:")
            )
            if data:
                value = json.loads(data)
                if isinstance(value, dict):
                    return value
        raise CorosAuthError("COROS MCP returned an invalid event stream")


class CorosReadClient:
    """Expose only the documented COROS activity-list read operation."""

    def __init__(self, transport: CorosMcpTransport) -> None:
        self._transport = transport

    def __call__(
        self, profile_id: UUID, since: datetime, until: datetime
    ) -> list[dict[str, Any]]:
        # COROS authorization is single-user. Profile routing is handled by the
        # composition root choosing the correctly authorized client.
        del profile_id
        return self.activities(since, until)

    def activities(self, since: datetime, until: datetime) -> list[dict[str, Any]]:
        if until < since:
            raise ValueError("until must not be before since")
        result = self._transport.call_tool(
            COROS_ACTIVITY_TOOL,
            {
                "startDate": since.strftime("%Y%m%d"),
                "endDate": until.strftime("%Y%m%d"),
                "sportTypeCodes": None,
                "minDistanceKm": None,
                "maxDistanceKm": None,
                "minDurationMinutes": None,
                "maxDurationMinutes": None,
                "maxAveragePace": None,
                "locationKeyword": None,
                "limit": _ACTIVITY_LIMIT,
            },
        )
        activities = _activity_list(result)
        if not all(isinstance(item, dict) for item in activities):
            raise ValueError("COROS response contains a malformed activity list")
        return activities


def _activity_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept the common direct and MCP structured-content response envelopes."""
    if result.get("isError") is True:
        raise RuntimeError("COROS activity query reported an error")
    candidate: object = result.get("activities")
    if candidate is None:
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            candidate = structured.get("activities", structured.get("records"))
    if candidate is None:
        candidate = result.get("records")
    if candidate is None:
        content = result.get("content")
        if (
            isinstance(content, list)
            and len(content) == 1
            and isinstance(content[0], dict)
        ):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError as error:
                    raise ValueError(
                        "COROS activity response text is not JSON encoded"
                    ) from error
                if not isinstance(decoded, str):
                    raise TypeError(
                        "COROS activity response text did not decode to text"
                    )
                candidate = _parse_sport_records(decoded)
    if not isinstance(candidate, list):
        raise TypeError("COROS response is missing an activity list")
    return candidate  # type: ignore[return-value]


def _parse_sport_records(text: str) -> list[dict[str, Any]]:
    if re.fullmatch(
        r"No sport records found from \d{4}-\d{2}-\d{2} to \d{4}-\d{2}-\d{2}\.",
        text,
    ):
        return []
    heading = re.match(
        r"^Sport Records — (\d{4}-\d{2}-\d{2}) to (\d{4}-\d{2}-\d{2}) \((\d+) records\)\r?\n={3,}\r?\n",
        text,
    )
    if heading is None:
        raise ValueError("COROS activity response has an invalid heading")
    expected = int(heading.group(3))
    blocks = (
        re.split(r"\n(?=\d+\. )", text[heading.end() :].strip()) if expected else []
    )
    activities = [_parse_record_block(block) for block in blocks if block.strip()]
    if len(activities) != expected:
        raise ValueError("COROS activity response record count did not match")
    if expected >= _ACTIVITY_LIMIT:
        for activity in activities:
            activity["query_may_be_truncated"] = True
    return activities


def _parse_record_block(block: str) -> dict[str, Any]:
    title = re.match(r"^\d+\. (.+) — (\d{4}-\d{2}-\d{2})$", block.splitlines()[0])
    label = re.search(r"(?m)^\s*LabelId:\s*(\d+)\s*\|\s*SportType:\s*(\d+)\s*$", block)
    window = re.search(r"startTimestamp=(\d+)\s*\|\s*endTimestamp=(\d+)", block)
    duration = re.search(r"Duration:\s*([0-9:]+)", block)
    distance = re.search(r"Distance:\s*([0-9]+(?:\.[0-9]+)?)\s*km", block)
    if title is None or label is None:
        raise ValueError("COROS activity response contains an invalid record")
    result: dict[str, Any] = {
        "id": label.group(1),
        "label_id": label.group(1),
        "source": "coros",
        "sport": title.group(1),
        "date": title.group(2),
        "sport_type": int(label.group(2)),
        "raw": block,
    }
    if window:
        start_timestamp = int(window.group(1))
        result["start_timestamp"] = start_timestamp
        result["end_timestamp"] = int(window.group(2))
        result["started_at"] = datetime.fromtimestamp(start_timestamp, UTC).isoformat()
    if duration:
        result["duration_s"] = _duration_seconds(duration.group(1))
    if distance:
        result["distance_km"] = float(distance.group(1))
    return result


def _duration_seconds(value: str) -> int:
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    raise ValueError("COROS activity duration is invalid")
