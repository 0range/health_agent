"""Small, read-only adapter for the official COROS MCP server.

OAuth and MCP session handling deliberately belong to the injected transport.  This
keeps access tokens out of the health-agent store and makes the boundary testable.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

import httpx

from health_agent.pilot.coros_auth import CorosAuthError, CorosOAuth

COROS_MCP_ENDPOINT = "https://mcp.coros.com/mcp"
COROS_ACTIVITY_TOOL = "querySportRecords"
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
        if self._request_id == 0:
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
        response = self._post(
            endpoint, headers, "tools/call", {"name": name, "arguments": arguments}
        )
        if "error" in response or not isinstance(response.get("result"), dict):
            raise CorosAuthError("COROS MCP tool call failed")
        return response["result"]  # type: ignore[return-value]

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
                "startDate": since.date().isoformat(),
                "endDate": until.date().isoformat(),
            },
        )
        activities = _activity_list(result)
        if not all(isinstance(item, dict) for item in activities):
            raise ValueError("COROS response contains a malformed activity list")
        return activities


def _activity_list(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Accept the common direct and MCP structured-content response envelopes."""
    candidate: object = result.get("activities")
    if candidate is None:
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            candidate = structured.get("activities", structured.get("records"))
    if candidate is None:
        candidate = result.get("records")
    if not isinstance(candidate, list):
        raise TypeError("COROS response is missing an activity list")
    return candidate  # type: ignore[return-value]
