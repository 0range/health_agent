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
        (
            "querySportRecords",
            {
                "startDate": "20260901",
                "endDate": "20260907",
                "sportTypeCodes": None,
                "minDistanceKm": None,
                "maxDistanceKm": None,
                "minDurationMinutes": None,
                "maxDurationMinutes": None,
                "maxAveragePace": None,
                "locationKeyword": None,
                "limit": 100,
            },
        )
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


def test_live_text_envelope_is_strictly_parsed_without_losing_raw_facts():
    raw = (
        "Sport Records — 2026-08-08 to 2026-09-07 (1 records)\r\n"
        "========================\r\n\r\n"
        "1. Outdoor Run — 2026-09-06\r\n"
        "   Location: Example Park\r\n"
        "   Start Coordinates: 00.000000, 00.000000\r\n"
        "   Time Window: startTimestamp=1788652800 | endTimestamp=1788654600\r\n"
        "   Duration: 30:00 | Distance: 5.00 km\r\n"
        "   Average Pace: 6:00 /km | Avg HR: 140 bpm | Calories: 300 kcal\r\n"
        "   LabelId: 123456789012345678 | SportType: 100"
    )

    class LiveShape:
        def call_tool(self, name, arguments):
            return {
                "content": [{"type": "text", "text": __import__("json").dumps(raw)}],
                "isError": False,
            }

    activity = CorosReadClient(LiveShape()).activities(
        datetime(2026, 8, 8, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
    )[0]
    assert activity["id"] == "123456789012345678"
    assert activity["source"] == "coros"
    assert activity["date"] == "2026-09-06"
    assert activity["start_timestamp"] == 1788652800
    assert activity["duration_s"] == 1800
    assert activity["distance_km"] == 5.0
    assert activity["started_at"] == "2026-09-06T00:00:00+00:00"
    assert activity["raw"].startswith("1. Outdoor Run")


def test_live_error_or_anomalous_text_is_not_silently_empty():
    class ErrorShape:
        def call_tool(self, name, arguments):
            return {
                "content": [{"type": "text", "text": '"service unavailable"'}],
                "isError": True,
            }

    with pytest.raises(RuntimeError, match="reported an error"):
        CorosReadClient(ErrorShape()).activities(
            datetime(2026, 8, 8, tzinfo=UTC), datetime(2026, 9, 7, tzinfo=UTC)
        )


def test_exact_live_empty_message_returns_empty_but_near_miss_raises():
    class EmptyShape:
        text = '"No sport records found from 2010-01-01 to 2010-01-31."'

        def call_tool(self, name, arguments):
            return {"content": [{"type": "text", "text": self.text}], "isError": False}

    source = EmptyShape()
    client = CorosReadClient(source)
    assert (
        client.activities(
            datetime(2010, 1, 1, tzinfo=UTC), datetime(2010, 1, 31, tzinfo=UTC)
        )
        == []
    )

    source.text = '"No sport records currently available."'
    with pytest.raises(ValueError, match="invalid heading"):
        client.activities(
            datetime(2010, 1, 1, tzinfo=UTC), datetime(2010, 1, 31, tzinfo=UTC)
        )
