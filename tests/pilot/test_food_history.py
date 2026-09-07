from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from health_agent.pilot.contracts import Record
from health_agent.pilot.food_history import NUTRIENTS, build_food_history


class MemoryStore:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, Record]] = []
        self.limits: list[int] = []

    def put(self, profile_id: UUID, domain: str, kind: str, key: str,
            payload: dict[str, Any], *, at: datetime | None = None) -> Record:
        record = Record(str(len(self.records)), domain, kind, key,
                        at or datetime.now(UTC), payload)
        self.records.append((profile_id, record))
        return record

    def list(self, profile_id: UUID, domain: str, kind: str | None = None,
             *, limit: int = 100) -> list[Record]:
        self.limits.append(limit)
        values = [record for owner, record in self.records
                  if owner == profile_id and record.domain == domain
                  and (kind is None or record.kind == kind)]
        return sorted(values, key=lambda record: record.at, reverse=True)[:limit]

    def get(self, profile_id: UUID, record_id: str) -> Record | None:
        return None

    def patch(self, profile_id: UUID, record_id: str,
              payload: dict[str, Any]) -> Record:
        raise NotImplementedError


def meal(occurred_at: str, **payload: Any) -> dict[str, Any]:
    return {"occurred_at": occurred_at, **payload}


def test_moscow_boundary_and_corrected_time_not_insertion_order() -> None:
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 7, 21, 30, tzinfo=UTC)  # 8 September in Moscow
    store.put(profile, "food", "meal", "inserted-newer",
              meal("2026-09-06T20:59:00+00:00", category="dinner"), at=now)
    store.put(profile, "food", "meal", "corrected-newer",
              meal("2026-09-07T21:00:00+00:00", category="breakfast"),
              at=now - timedelta(days=5))

    result = build_food_history(store, profile, now, days=1)

    assert result["period"] == {
        "days": 1, "from_date": "2026-09-08", "to_date": "2026-09-08"
    }
    assert result["recorded_days"] == ["2026-09-08"]
    assert [item["category"] for item in result["meals"]] == ["breakfast"]


def test_fixed_projection_numbers_end_estimate_and_no_private_leak_or_mutation() -> None:
    store, profile = MemoryStore(), uuid4()
    payload = meal(
        "2026-09-07T10:00:00+03:00", category="lunch",
        ended_at="2026-09-07T10:20:00+03:00", end_source="last_photo_plus_20m",
        photo_path="/private/plate.jpg", original="private conversation",
        analysis={
            "foods": [" soup ", 7, "x" * 250], "portion_estimate": " bowl ",
            "kcal": 0, "protein_g": None, "fat_g": True, "carbs_g": float("inf"),
            "saturated_fat_g": -1, "fiber_g": 2.5, "cholesterol_mg": "12",
            "unknowns": [" salt ", {"private": "value"}],
            "feedback": "do not expose", "arbitrary": "do not expose",
        },
    )
    before = repr(payload)
    store.put(profile, "food", "meal", "m", payload)

    result = build_food_history(store, profile, datetime(2026, 9, 7, 7, 5, tzinfo=UTC))
    projected = result["meals"][0]
    assert projected["nutrients_estimated"] == dict(
        zip(NUTRIENTS, [0, None, None, None, None, 2.5, None], strict=True)
    )
    assert projected["foods"] == ["soup", "x" * 200]
    assert projected["unknowns"] == ["salt"]
    assert projected["ended_at"] == "2026-09-07T10:20:00+03:00"
    assert projected["end_source"] == "last_photo_plus_20m"
    assert "private" not in str(result) and "feedback" not in str(result)
    assert repr(payload) == before


@pytest.mark.parametrize(
    ("ended_at", "end_source"),
    [
        ("2026-09-07T10:20:00+03:00", None),
        ("2026-09-07T10:20:00+03:00", "comment_time"),
        ("malformed", "last_photo_plus_20m"),
        (None, "last_photo_plus_20m"),
    ],
)
def test_end_estimate_is_only_exposed_as_a_validated_pair(
    ended_at: str | None, end_source: str | None
) -> None:
    store, profile = MemoryStore(), uuid4()
    store.put(
        profile,
        "food",
        "meal",
        "m",
        meal(
            "2026-09-07T10:00:00+03:00",
            ended_at=ended_at,
            end_source=end_source,
        ),
    )
    projected = build_food_history(
        store, profile, datetime(2026, 9, 7, 8, tzinfo=UTC)
    )["meals"][0]
    assert "ended_at" not in projected
    assert "end_source" not in projected


def test_empty_invalid_profiles_bounds_and_truncation() -> None:
    store, own, other = MemoryStore(), uuid4(), uuid4()
    now = datetime(2026, 9, 7, 10, tzinfo=UTC)
    assert build_food_history(store, own, now, days=2)["recorded_meal_count"] == 0
    store.put(own, "food", "meal", "bad", meal("not-a-date"))
    store.put(other, "food", "meal", "other", meal(now.isoformat(), original="secret"))
    for index in range(101):
        store.put(own, "food", "meal", str(index),
                  meal((now - timedelta(minutes=index)).isoformat(), analysis={}),
                  at=now - timedelta(days=index))

    result = build_food_history(store, own, now)
    assert 0 < result["recorded_meal_count"] <= 100 and result["truncated"] is True
    assert result["invalid_record_count"] == 1 and store.limits[-1] == 1000
    assert "secret" not in str(result)


def test_projection_has_hard_json_context_bound() -> None:
    store, profile = MemoryStore(), uuid4()
    now = datetime(2026, 9, 7, 10, tzinfo=UTC)
    analysis = {"foods": ["f" * 200] * 50, "unknowns": ["u" * 200] * 50}
    for index in range(100):
        store.put(profile, "food", "meal", str(index),
                  meal((now - timedelta(minutes=index)).isoformat(), analysis=analysis))
    result = build_food_history(store, profile, now)
    assert len(json.dumps(result, ensure_ascii=False)) <= 20_000
    assert result["truncated"] is True


@pytest.mark.parametrize("days", [0, 32, True])
def test_validation(days: Any) -> None:
    with pytest.raises(ValueError):
        build_food_history(MemoryStore(), uuid4(), datetime.now(UTC), days=days)
    with pytest.raises(ValueError):
        build_food_history(
            MemoryStore(), uuid4(), datetime(2026, 9, 7), days=1  # noqa: DTZ001
        )
