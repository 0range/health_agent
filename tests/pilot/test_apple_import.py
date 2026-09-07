from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from health_agent.pilot.apple_import import import_apple_export
from health_agent.pilot.contracts import Record


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


def _write(tmp_path: Path, children: str, *, dtd: str = "") -> Path:
    path = tmp_path / "export.xml"
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        + dtd
        + '<HealthData locale="en_US">\n'
        + children
        + "\n</HealthData>\n"
    )
    return path


def _future(days: int = 3) -> str:
    return (datetime.now(UTC) + timedelta(days=days)).isoformat()


def _weight(value="80", unit="kg", when="2026-01-02T07:00:00+03:00"):
    return (
        '<Record type="HKQuantityTypeIdentifierBodyMass" '
        f'sourceName="Apple Health" unit="{unit}" value="{value}" '
        f'startDate="{when}" endDate="{when}">'
        '<MetadataEntry key="HKWasUserEntered" value="1"/>'
        "</Record>"
    )


def _workout(
    source="Apple Watch",
    start="2026-01-03T07:00:00+03:00",
    end="2026-01-03T08:00:00+03:00",
):
    return (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        'duration="60" durationUnit="min" '
        f'sourceName="{source}" startDate="{start}" endDate="{end}">'
        '<MetadataEntry key="Indoor" value="0"/>'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" '
        f'startDate="{start}" endDate="{end}" average="145" unit="count/min"/>'
        "</Workout>"
    )


def test_imports_supported_weights_workouts_metadata_and_counts_other_types(tmp_path):
    store = MemoryStore()
    profile = uuid4()
    export = _write(
        tmp_path,
        _weight("176.3696", "lb")
        + '<Record type="HKQuantityTypeIdentifierStepCount" sourceName="Phone" '
        'value="4" unit="count" startDate="2026-01-01T00:00:00+00:00" '
        'endDate="2026-01-01T00:01:00+00:00"/>'
        + _workout("COROS"),
    )

    counts = import_apple_export(store, profile, export)

    weight = next(r for r in store.records.values() if r.kind == "weight")
    workout = next(r for r in store.records.values() if r.kind == "apple_workout")
    manifest = next(r for r in store.records.values() if r.kind == "apple_import")
    assert counts == {
        "records_seen": 2,
        "workouts_seen": 1,
        "weights_imported": 1,
        "workouts_imported": 1,
        "reviewed": 0,
    }
    assert weight.payload["weight_kg"] == pytest.approx(80.0, abs=0.001)
    assert weight.payload["original_value"] == "176.3696"
    assert weight.payload["metadata"] == {"HKWasUserEntered": "1"}
    assert workout.payload["duration_seconds"] == 3600
    assert workout.payload["potential_copy"] is True
    assert workout.payload["raw_xml"]["children"][1]["tag"] == "WorkoutStatistics"
    assert manifest.payload["record_type_counts"] == {
        "HKQuantityTypeIdentifierBodyMass": 1,
        "HKQuantityTypeIdentifierStepCount": 1,
    }
    assert manifest.payload["per_type"]["HKQuantityTypeIdentifierBodyMass"][
        "earliest"
    ].startswith("2026-01-02")


def test_invalid_units_dates_future_and_reversed_workouts_go_to_review(tmp_path):
    future = _future()
    export = _write(
        tmp_path,
        _weight("80", "stone")
        + _weight("nan", "kg")
        + _weight("80", "kg", future)
        + _workout(end="2025-01-03T08:00:00+03:00")
        + _workout(start=future, end=_future(4)),
    )
    store = MemoryStore()

    counts = import_apple_export(store, uuid4(), export)

    reviews = [r for r in store.records.values() if r.kind == "apple_review"]
    assert counts["reviewed"] == 5
    assert counts["weights_imported"] == 0
    assert counts["workouts_imported"] == 0
    assert {r.payload["reason"] for r in reviews} == {
        "unsupported_weight_unit",
        "invalid_weight",
        "future_timestamp",
        "workout_end_before_start",
    }


def test_manifest_separates_future_and_invalid_dates_from_valid_coverage(tmp_path):
    future = _future()
    export = _write(
        tmp_path,
        '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" '
        'startDate="not-a-date" endDate="not-a-date" value="awake"/>'
        '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" '
        f'startDate="{future}" endDate="{future}" value="asleep"/>'
        '<Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Watch" '
        'startDate="2026-01-01T00:00:00+00:00" '
        'endDate="2026-01-01T01:00:00+00:00" value="asleep"/>',
    )
    store = MemoryStore()

    import_apple_export(store, uuid4(), export)

    manifest = next(r for r in store.records.values() if r.kind == "apple_import")
    coverage = manifest.payload["per_type"][
        "HKCategoryTypeIdentifierSleepAnalysis"
    ]
    assert coverage["invalid_timestamp_count"] == 1
    assert coverage["future_timestamp_count"] == 1
    assert coverage["earliest"] == coverage["latest"]
    assert coverage["latest"].startswith("2026-01-01")
    assert coverage["raw_latest"] == future


def test_exact_replay_and_profiles_are_isolated(tmp_path):
    export = _write(tmp_path, _weight() + _workout())
    store = MemoryStore()
    first_profile = uuid4()
    second_profile = uuid4()

    first = import_apple_export(store, first_profile, export)
    initial = len(store.records)
    again = import_apple_export(store, first_profile, export)
    import_apple_export(store, second_profile, export)

    assert again == first
    assert len(store.records) == initial * 2
    keys = [key[3] for key in store.records if key[2] != "apple_import"]
    assert all(key.startswith("apple:") for key in keys)


def test_nested_correlation_record_is_not_counted_or_imported(tmp_path):
    export = _write(
        tmp_path,
        '<Correlation type="example" sourceName="Phone" '
        'startDate="2026-01-01T00:00:00+00:00" '
        'endDate="2026-01-01T00:00:00+00:00">'
        + _weight()
        + "</Correlation>",
    )
    store = MemoryStore()

    counts = import_apple_export(store, uuid4(), export)

    assert counts["records_seen"] == 0
    assert counts["weights_imported"] == 0


def test_external_entity_declarations_are_rejected(tmp_path):
    export = _write(
        tmp_path,
        _weight(),
        dtd='<!DOCTYPE HealthData [<!ENTITY leaked SYSTEM "file:///etc/passwd">]>\n',
    )

    with pytest.raises(ValueError, match="external entities"):
        import_apple_export(MemoryStore(), uuid4(), export)


def test_normal_internal_apple_dtd_is_accepted(tmp_path):
    export = _write(
        tmp_path,
        _weight(),
        dtd="<!DOCTYPE HealthData [<!ELEMENT HealthData (Record*)>\n"
        "<!ELEMENT Record (MetadataEntry*)>\n"
        "<!ATTLIST Record type CDATA #REQUIRED>\n"
        "<!ELEMENT MetadataEntry EMPTY>]>",
    )

    assert import_apple_export(MemoryStore(), uuid4(), export)[
        "weights_imported"
    ] == 1
