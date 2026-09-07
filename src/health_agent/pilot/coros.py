"""Small, read-only adapter for the official COROS MCP server.

OAuth and MCP session handling deliberately belong to the injected transport.  This
keeps access tokens out of the health-agent store and makes the boundary testable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

COROS_MCP_ENDPOINT = "https://mcp.coros.com/mcp"
COROS_ACTIVITY_TOOL = "querySportRecords"


class CorosMcpTransport(Protocol):
    """An authenticated MCP transport (for example an OAuth-aware MCP client)."""

    def call_tool(self, name: str, arguments: dict[str, object]) -> dict[str, Any]: ...


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
