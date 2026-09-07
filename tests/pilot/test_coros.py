from datetime import UTC, datetime

import pytest

from health_agent.pilot.coros import COROS_MCP_ENDPOINT, CorosReadClient


class Transport:
    def __init__(self):
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"activities": [{"id": "42", "sport": "run"}]}


def test_activity_adapter_uses_official_read_tool_only():
    transport = Transport()
    client = CorosReadClient(transport)
    result = client.activities(
        datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    )
    assert result == [{"id": "42", "sport": "run"}]
    assert transport.calls == [
        ("querySportRecords", {"startDate": "2026-09-01", "endDate": "2026-09-07"})
    ]
    assert all(
        "write" not in name.lower()
        and not name.lower().startswith(("generate", "update"))
        for name, _ in transport.calls
    )
    assert COROS_MCP_ENDPOINT == "https://mcp.coros.com/mcp"


def test_client_rejects_unknown_or_malformed_results():
    class Bad:
        def call_tool(self, name, arguments):
            return {"activities": "not-a-list"}

    with pytest.raises(TypeError, match="activity list"):
        CorosReadClient(Bad()).activities(
            datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
        )


def test_client_is_callable_as_training_activity_source():
    transport = Transport()
    profile = __import__("uuid").uuid4()
    result = CorosReadClient(transport)(
        profile, datetime(2026, 9, 1, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    )
    assert result[0]["id"] == "42"
