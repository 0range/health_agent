"""Bounded-memory import of selected records from a local Apple Health export."""

from __future__ import annotations

import hashlib
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID
from xml.etree import ElementTree as ET

from health_agent.pilot.contracts import Store

BODY_MASS = "HKQuantityTypeIdentifierBodyMass"
_ENTITY_DECLARATION = re.compile(br"<!ENTITY\s", re.IGNORECASE)
_COPY_SOURCES = ("coros", "whoop", "oura")
_DURATION_FACTORS = {
    "s": 1.0,
    "sec": 1.0,
    "second": 1.0,
    "seconds": 1.0,
    "min": 60.0,
    "minute": 60.0,
    "minutes": 60.0,
    "h": 3600.0,
    "hr": 3600.0,
    "hour": 3600.0,
    "hours": 3600.0,
}


class _HashingSafeReader:
    """Hash input as expat consumes it and reject entity declarations."""

    def __init__(self, source: BinaryIO) -> None:
        self.source = source
        self.digest = hashlib.sha256()
        self.tail = b""

    def read(self, size: int = -1) -> bytes:
        data = self.source.read(size)
        if data:
            self.digest.update(data)
            scanned = self.tail + data
            if _ENTITY_DECLARATION.search(scanned):
                raise ValueError("Apple Health XML external entities are not allowed")
            self.tail = scanned[-16:]
        return data


def import_apple_export(
    store: Store, profile_id: UUID, export_file: Path
) -> dict[str, int]:
    """Import body mass and workouts, retaining all other data in the source file."""
    source_path = Path(export_file).resolve(strict=True)
    counts = {
        "records_seen": 0,
        "workouts_seen": 0,
        "weights_imported": 0,
        "workouts_imported": 0,
        "reviewed": 0,
    }
    record_types: dict[str, int] = {}
    per_type: dict[str, dict[str, Any]] = {}
    now = datetime.now(UTC)

    with source_path.open("rb") as source:
        reader = _HashingSafeReader(source)
        stack: list[ET.Element] = []
        for event, elem in ET.iterparse(reader, events=("start", "end")):
            if event == "start":
                stack.append(elem)
                continue
            parent = stack[-2] if len(stack) > 1 else None
            if parent is not None and parent.tag == "HealthData":
                if elem.tag == "Record":
                    counts["records_seen"] += 1
                    record_type = elem.attrib.get("type", "")
                    record_types[record_type] = record_types.get(record_type, 0) + 1
                    _observe_type(
                        per_type, record_type, elem.attrib.get("startDate"), now
                    )
                    if record_type == BODY_MASS:
                        _import_weight(store, profile_id, elem, now, counts)
                elif elem.tag == "Workout":
                    counts["workouts_seen"] += 1
                    workout_type = elem.attrib.get("workoutActivityType", "Workout")
                    _observe_type(
                        per_type, workout_type, elem.attrib.get("startDate"), now
                    )
                    _import_workout(store, profile_id, elem, now, counts)
                parent.remove(elem)
                elem.clear()
            stack.pop()

        file_hash = reader.digest.hexdigest()

    manifest = {
        "source": "apple_health",
        "source_path": str(source_path),
        "file_sha256": file_hash,
        "counts": dict(counts),
        "record_type_counts": record_types,
        "per_type": per_type,
    }
    store.put(
        profile_id,
        "shared",
        "apple_import",
        f"apple-import:{file_hash}",
        manifest,
        at=now,
    )
    return counts


def _import_weight(
    store: Store,
    profile_id: UUID,
    elem: ET.Element,
    now: datetime,
    counts: dict[str, int],
) -> None:
    raw = _raw_element(elem)
    source_key = _source_key(elem)
    recorded_at = _parse_datetime(elem.attrib.get("startDate"))
    unit = elem.attrib.get("unit", "")
    value_text = elem.attrib.get("value", "")
    reason = None
    try:
        value = float(value_text)
    except (TypeError, ValueError):
        value = math.nan
    if not math.isfinite(value) or value <= 0:
        reason = "invalid_weight"
    elif unit not in {"kg", "lb"}:
        reason = "unsupported_weight_unit"
    elif recorded_at is None:
        reason = "invalid_timestamp"
    elif recorded_at.astimezone(UTC) > now:
        reason = "future_timestamp"

    base: dict[str, Any] = {
        "source": "apple_health",
        "upstream_source": elem.attrib.get("sourceName"),
        "recorded_at": elem.attrib.get("startDate"),
        "original_value": value_text,
        "original_unit": unit,
        "metadata": _metadata(elem),
        "raw_xml": raw,
    }
    if reason is not None:
        _review(store, profile_id, source_key, "weight", reason, base, now)
        counts["reviewed"] += 1
        return
    base["weight_kg"] = value if unit == "kg" else value * 0.45359237
    store.put(profile_id, "shared", "weight", source_key, base, at=recorded_at)
    counts["weights_imported"] += 1


def _import_workout(
    store: Store,
    profile_id: UUID,
    elem: ET.Element,
    now: datetime,
    counts: dict[str, int],
) -> None:
    source_key = _source_key(elem)
    start = _parse_datetime(elem.attrib.get("startDate"))
    end = _parse_datetime(elem.attrib.get("endDate"))
    duration = _duration_seconds(elem)
    metadata = _metadata(elem)
    provenance_text = " ".join(
        [elem.attrib.get("sourceName", ""), *metadata.keys(), *metadata.values()]
    ).casefold()
    payload = {
        "source": "apple_health",
        "upstream_source": elem.attrib.get("sourceName"),
        "started_at": elem.attrib.get("startDate"),
        "ended_at": elem.attrib.get("endDate"),
        "activity_type": elem.attrib.get("workoutActivityType"),
        "duration_seconds": duration,
        "potential_copy": any(name in provenance_text for name in _COPY_SOURCES),
        "metadata": metadata,
        "raw_xml": _raw_element(elem),
    }
    reason = None
    if start is None or end is None:
        reason = "invalid_timestamp"
    elif start.astimezone(UTC) > now or end.astimezone(UTC) > now:
        reason = "future_timestamp"
    elif end < start:
        reason = "workout_end_before_start"
    elif duration is None:
        reason = "invalid_duration"
    if reason is not None:
        _review(store, profile_id, source_key, "apple_workout", reason, payload, now)
        counts["reviewed"] += 1
        return
    store.put(
        profile_id, "training", "apple_workout", source_key, payload, at=start
    )
    counts["workouts_imported"] += 1


def _review(
    store: Store,
    profile_id: UUID,
    source_key: str,
    intended_kind: str,
    reason: str,
    raw_payload: dict[str, Any],
    now: datetime,
) -> None:
    store.put(
        profile_id,
        "shared",
        "apple_review",
        source_key,
        {
            "source": "apple_health",
            "intended_kind": intended_kind,
            "reason": reason,
            "raw": raw_payload,
        },
        at=now,
    )


def _duration_seconds(elem: ET.Element) -> float | None:
    text = elem.attrib.get("duration")
    unit = elem.attrib.get("durationUnit", "s").casefold()
    try:
        value = float(text) if text is not None else None
    except ValueError:
        return None
    factor = _DURATION_FACTORS.get(unit)
    if value is None or factor is None or not math.isfinite(value) or value < 0:
        return None
    return value * factor


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _metadata(elem: ET.Element) -> dict[str, str]:
    return {
        child.attrib.get("key", ""): child.attrib.get("value", "")
        for child in elem
        if child.tag == "MetadataEntry"
    }


def _raw_element(elem: ET.Element) -> dict[str, Any]:
    return {
        "tag": elem.tag,
        "attributes": dict(elem.attrib),
        "children": [_raw_element(child) for child in elem],
    }


def _canonical_element(elem: ET.Element) -> bytes:
    parts = [b"<", elem.tag.encode(), b">"]
    for key, value in sorted(elem.attrib.items()):
        parts.extend((key.encode(), b"=", value.encode(), b"\0"))
    if elem.text and elem.text.strip():
        parts.append(elem.text.strip().encode())
    for child in elem:
        parts.append(_canonical_element(child))
    parts.extend((b"</", elem.tag.encode(), b">"))
    return b"".join(parts)


def _source_key(elem: ET.Element) -> str:
    return "apple:" + hashlib.sha256(_canonical_element(elem)).hexdigest()


def _observe_type(
    per_type: dict[str, dict[str, Any]],
    kind: str,
    value: str | None,
    now: datetime,
) -> None:
    bucket = per_type.setdefault(
        kind,
        {
            "count": 0,
            "invalid_timestamp_count": 0,
            "future_timestamp_count": 0,
            "earliest": None,
            "latest": None,
            "raw_latest": None,
        },
    )
    bucket["count"] += 1
    parsed = _parse_datetime(value)
    if parsed is None:
        bucket["invalid_timestamp_count"] += 1
        return
    raw_latest = _parse_datetime(bucket["raw_latest"])
    if raw_latest is None or parsed > raw_latest:
        bucket["raw_latest"] = value
    if parsed.astimezone(UTC) > now:
        bucket["future_timestamp_count"] += 1
        return
    iso = parsed.isoformat()
    if bucket["earliest"] is None or parsed < datetime.fromisoformat(bucket["earliest"]):
        bucket["earliest"] = iso
    if bucket["latest"] is None or parsed > datetime.fromisoformat(bucket["latest"]):
        bucket["latest"] = iso
