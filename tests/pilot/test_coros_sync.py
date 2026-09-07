import json
import stat
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from health_agent.pilot.contracts import Record
from health_agent.pilot.coros_sync import CorosSync


class MemoryStore:
    def __init__(self):
        self.records = {}

    def put(self, profile_id, domain, kind, source_key, payload, *, at=None):
        key = (profile_id, domain, kind, source_key)
        if key not in self.records:
            self.records[key] = Record(
                str(uuid4()), domain, kind, source_key, at, payload
            )
        return self.records[key]


class Transport:
    def __init__(self, responder):
        self.responder = responder
        self.calls = []

    def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"activities": self.responder(arguments)}


def activity(identifier, day="2026-01-02"):
    return {"id": str(identifier), "started_at": day + "T07:00:00+03:00"}


def iso_day(value):
    return value if "-" in value else f"{value[:4]}-{value[4:6]}-{value[6:]}"


def build(tmp_path, transport, store=None):
    return CorosSync(
        store or MemoryStore(),
        object(),
        uuid4(),
        tmp_path,
        transport=transport,
        clock=lambda: datetime(2026, 9, 7, 12, tzinfo=UTC),
    )


def test_sync_uses_month_windows_archives_raw_and_replays_unchanged(tmp_path):
    store = MemoryStore()
    transport = Transport(lambda args: [activity(iso_day(args["startDate"]))])
    sync = build(tmp_path, transport, store)

    first = sync.sync(date(2026, 1, 20), date(2026, 3, 2))
    original = next(
        record
        for key, record in store.records.items()
        if key[2:4] == ("activity", "activity:2026-01-20")
    )
    sync.sync(date(2026, 1, 20), date(2026, 3, 2))
    replayed = store.records[
        (sync.profile_id, "training", "activity", "activity:2026-01-20")
    ]

    assert first == {"requests": 3, "fetched": 3, "stored": 3, "incomplete": 0}
    assert replayed == original
    assert [
        (iso_day(call[1]["startDate"]), iso_day(call[1]["endDate"]))
        for call in transport.calls[:3]
    ] == [
        ("2026-01-20", "2026-01-31"),
        ("2026-02-01", "2026-02-28"),
        ("2026-03-01", "2026-03-02"),
    ]
    raw = sorted((tmp_path / "raw").glob("*.json"))
    assert len(raw) == 6
    archives = [json.loads(path.read_text()) for path in raw]
    assert any(
        item["requested_range"]["since"] in {"2026-01-20", "20260120"}
        for item in archives
    )
    assert all(item["payload"]["activities"] for item in archives)
    assert stat.S_IMODE(raw[0].stat().st_mode) == 0o600


def test_limit_is_split_and_saturated_day_is_explicitly_incomplete(tmp_path):
    def respond(args):
        start = iso_day(args["startDate"])
        end = iso_day(args["endDate"])
        if start == end == "2026-01-01":
            return [activity(index, "2026-01-01") for index in range(100)]
        if start == end == "2026-01-02":
            return [activity(100, "2026-01-02")]
        return [activity(index, "2026-01-01") for index in range(100)]

    sync = build(tmp_path, Transport(respond))
    counts = sync.sync(date(2026, 1, 1), date(2026, 1, 2))

    assert counts == {"requests": 3, "fetched": 101, "stored": 101, "incomplete": 1}
    run = next(r for r in sync.store.records.values() if r.kind == "sync_run")
    assert run.payload["status"] == "incomplete"


def test_failure_preserves_prior_activities_and_records_safe_failure(tmp_path):
    class Broken(Transport):
        def call_tool(self, name, arguments):
            if iso_day(arguments["startDate"]) == "2026-02-01":
                raise RuntimeError("secret must not be persisted")
            return super().call_tool(name, arguments)

    store = MemoryStore()
    sync = build(tmp_path, Broken(lambda args: [activity("kept")]), store)

    with pytest.raises(RuntimeError):
        sync.sync(date(2026, 1, 1), date(2026, 2, 2))

    assert any(r.source_key == "activity:kept" for r in store.records.values())
    run = next(r for r in store.records.values() if r.kind == "sync_run")
    assert run.payload["status"] == "failed"
    assert run.payload["failure"] == "RuntimeError"
    assert "secret" not in json.dumps(run.payload)


def test_archives_redact_token_shaped_fields(tmp_path):
    class TokenTransport:
        def call_tool(self, name, arguments):
            return {
                "activities": [activity("one")],
                "access_token": "do-not-save",
            }

    build(tmp_path, TokenTransport()).sync(date(2026, 1, 1), date(2026, 1, 1))
    archived = json.loads(next((tmp_path / "raw").glob("*.json")).read_text())
    assert archived["payload"]["access_token"] == "[redacted]"
    assert "do-not-save" not in json.dumps(archived)
