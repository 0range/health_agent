"""Private daily comparison exports; native measurements remain authoritative."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, or_, select

from health_agent.automation.storage import atomic_private_write
from health_agent.config import Settings
from health_agent.db import session_scope
from health_agent.pilot.storage import PilotRecord
from health_agent.qingping.service import FIELDS, Connection
from health_agent.research.calendar import STUDY_START, ZONE, bounds
from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopConnection,
    WhoopCycle,
    WhoopRecovery,
    WhoopSleep,
    WhoopWorkout,
)
from health_agent.whoop.participants import targets


def csv_bytes(rows: list[dict[str, Any]], fields: list[str]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode()


def interval_summary(
    points: list[tuple[datetime, float]], start: datetime, end: datetime
) -> dict[int, dict[str, Any]]:
    bins: dict[int, list[tuple[datetime, float]]] = defaultdict(list)
    for at, value in points:
        if (
            start <= at < end
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            bins[int((at - start).total_seconds() // 6)].append((at, value))
    return {
        key: {
            "count": len(values),
            "mean": sum(v for _, v in values) / len(values),
            "min": min(v for _, v in values),
            "max": max(v for _, v in values),
            "first_at_utc": min(t for t, _ in values).isoformat(),
            "last_at_utc": max(t for t, _ in values).isoformat(),
        }
        for key, values in bins.items()
    }


def stress_rows(payload: dict[str, Any], participant: str) -> list[dict[str, Any]]:
    result = []
    raw = payload.get("raw", {})
    for graph_name in (
        "stress_graph",
        "extended24_hour_graph",
        "previous_stress_graph",
    ):
        graph = raw.get(graph_name) or {}
        for plot in graph.get("graph", {}).get("plots", []):
            if plot.get("type") != "LINE_PLOT":
                continue
            for segment_index, segment in enumerate(
                plot.get("plot", {}).get("segments", [])
            ):
                for point_index, point in enumerate(segment.get("points", [])):
                    detail = point.get("data_scrubber_details") or {}
                    result.append(
                        {
                            "participant": participant,
                            "source_date": payload.get("logical_id"),
                            "graph": graph_name,
                            "segment": segment_index,
                            "point": point_index,
                            "native_time_label": detail.get(
                                "primary_contextual_display"
                            ),
                            "displayed_score": detail.get("value_display"),
                            "position_x": point.get("position_x"),
                            "position_y": point.get("position_y"),
                            "at_utc": "",
                            "time_alignment": "unverified_native_graph",
                            "captured_at": payload.get(
                                "last_seen_at", payload.get("captured_at")
                            ),
                        }
                    )
    return result


def export_day(
    settings: Settings,
    engine: Engine,
    sensor: Connection,
    day: date,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    if day < STUDY_START or day > now.astimezone(ZONE).date():
        raise ValueError("date_outside_study")
    start, end = bounds(day)
    enrolled = targets(settings)
    profiles = [t.profile_id or sensor.profile_id for t in enrolled]
    if len(set(profiles)) != len(profiles):
        raise ValueError("duplicate_participant")
    root = settings.research_root / "datasets" / day.isoformat()
    native: list[dict[str, Any]] = []
    series: dict[str, list[tuple[datetime, float]]] = {f"air_{k}": [] for k in FIELDS}
    participants: list[dict[str, Any]] = []
    summaries: dict[str, Any] = {}
    stresses: list[dict[str, Any]] = []
    room_bins: dict[int, set[str]] = defaultdict(set)
    with session_scope(engine) as session:
        air = session.scalars(
            select(PilotRecord)
            .where(
                PilotRecord.profile_id == sensor.profile_id,
                PilotRecord.kind == "room_measurement",
                PilotRecord.domain == "shared",
                PilotRecord.payload["device_mac"].astext == sensor.mac,
                PilotRecord.at >= start,
                PilotRecord.at < end,
            )
            .order_by(PilotRecord.at)
        ).all()
        for row in air:
            room = row.payload.get("room", "")
            room_bins[int((row.at - start).total_seconds() // 6)].add(room)
            for metric, item in row.payload.get("metrics", {}).items():
                value = item.get("value")
                native.append(
                    {
                        "source": "air",
                        "participant": "shared_room",
                        "at_utc": row.at.isoformat(),
                        "metric": metric,
                        "value": value,
                        "unit": item.get("unit"),
                        "room": room,
                    }
                )
                if metric in FIELDS:
                    series[f"air_{metric}"].append((row.at, value))
        for index, profile in enumerate(profiles, 1):
            alias = f"person_{index}"
            connection = session.scalar(
                select(WhoopConnection).where(WhoopConnection.profile_id == profile)
            )
            if connection is None or connection.external_user_id is None:
                raise ValueError("participant_not_connected")
            details = session.scalars(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile,
                    PilotRecord.kind == "whoop_detail",
                    PilotRecord.payload["external_user_id"].astext
                    == str(connection.external_user_id),
                    PilotRecord.payload["calendar_timezone"].astext == str(ZONE),
                    PilotRecord.payload["logical_id"].astext == day.isoformat(),
                    PilotRecord.at == start,
                )
            ).all()
            latest = {}
            for row in sorted(
                details,
                key=lambda r: r.payload.get(
                    "last_seen_at", r.payload.get("captured_at", "")
                ),
            ):
                latest[row.payload["resource"]] = row.payload
            points = []
            for point in latest.get("heart_rate", {}).get("raw", {}).get("values", []):
                at = datetime.fromtimestamp(point["time"] / 1000, UTC)
                if start <= at < end:
                    points.append((at, point["data"]))
                    native.append(
                        {
                            "source": "whoop",
                            "participant": alias,
                            "at_utc": at.isoformat(),
                            "metric": "heart_rate",
                            "value": point["data"],
                            "unit": "bpm",
                            "room": "",
                        }
                    )
            series[f"{alias}_heart_rate"] = points
            stresses.extend(stress_rows(latest.get("stress", {}), alias))
            sleeps = session.scalars(
                select(WhoopSleep).where(
                    WhoopSleep.connection_id == connection.id,
                    WhoopSleep.start_at < end,
                    WhoopSleep.end_at > start,
                )
            ).all()
            sleep_ids = [s.external_id for s in sleeps]
            recoveries = session.scalars(
                select(WhoopRecovery).where(
                    WhoopRecovery.connection_id == connection.id,
                    WhoopRecovery.sleep_id.in_(sleep_ids),
                )
            ).all()
            stages = session.scalars(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile,
                    PilotRecord.kind == "whoop_detail",
                    PilotRecord.payload["external_user_id"].astext
                    == str(connection.external_user_id),
                    PilotRecord.payload["resource"].astext == "sleep_stages",
                    PilotRecord.payload["logical_id"].astext.in_(sleep_ids),
                )
            ).all()
            stage_latest = {}
            for row in sorted(
                stages,
                key=lambda r: r.payload.get(
                    "last_seen_at", r.payload.get("captured_at", "")
                ),
            ):
                stage_latest[row.payload["logical_id"]] = row.payload
            summary = {
                "sleep": [s.source_values for s in sleeps],
                "recovery": [r.source_values for r in recoveries],
                "sleep_stages": stage_latest,
                "stress_original": latest.get("stress", {}),
                "heart_rate_provenance": {
                    k: v for k, v in latest.get("heart_rate", {}).items() if k != "raw"
                },
            }
            resource_models: list[tuple[str, Any]] = [
                ("cycles", WhoopCycle),
                ("workouts", WhoopWorkout),
            ]
            for name, model in resource_models:
                summary[name] = [
                    r.source_values
                    for r in session.scalars(
                        select(model).where(
                            model.connection_id == connection.id,
                            model.start_at < end,
                            or_(model.end_at.is_(None), model.end_at > start),
                        )
                    )
                ]
            summary["body_current"] = [
                {
                    "observed_at": b.observed_at.isoformat(),
                    "source_values": b.source_values,
                }
                for b in session.scalars(
                    select(WhoopBodyCurrent).where(
                        WhoopBodyCurrent.connection_id == connection.id
                    )
                )
            ]
            summaries[alias] = summary
            participants.append(
                {
                    "column_prefix": alias,
                    "profile_id": str(profile),
                    "heart_rate_points": len(points),
                    "stress_available": bool(latest.get("stress")),
                    "sleep_records": len(sleeps),
                }
            )
    binned = {
        name: interval_summary(points, start, end) for name, points in series.items()
    }
    stats = ["count", "mean", "min", "max", "first_at_utc", "last_at_utc"]
    fields = ["interval_start_utc", "interval_end_utc", "room_labels"] + [
        f"{name}_{stat}" for name in series for stat in stats
    ]
    rows: list[dict[str, Any]] = []
    for i in range(14400):
        comparison_row = {
            "interval_start_utc": (start + timedelta(seconds=i * 6)).isoformat(),
            "interval_end_utc": (start + timedelta(seconds=(i + 1) * 6)).isoformat(),
            "room_labels": "|".join(sorted(room_bins.get(i, set()))),
        }
        for name, bins in binned.items():
            comparison_row.update(
                {
                    f"{name}_{stat}": bins.get(i, {}).get(
                        stat, 0 if stat == "count" else ""
                    )
                    for stat in stats
                }
            )
        rows.append(comparison_row)
    manifest = {
        "date": day.isoformat(),
        "timezone": str(ZONE),
        "generated_at": now.isoformat(),
        "partial_day": now < end,
        "interval_seconds": 6,
        "rows": len(rows),
        "participants": participants,
        "air_samples": len(air),
        "air_units": FIELDS,
        "aggregation": "half-open UTC intervals; no interpolation; count zero means no observation",
        "stress_time_alignment": "unverified; native minute labels only, never synthetic UTC",
        "nightly_metrics": "original sleep/recovery source values, not continuous samples",
        "noise_max": "maximum of returned samples, not manufacturer Lmax or guaranteed acoustic peak",
        "completeness": "exported does not mean complete; consult research/daily/<profile-id>/<date>.json",
    }
    atomic_private_write(root / "series-6s.csv", csv_bytes(rows, fields))
    atomic_private_write(
        root / "native-observations.csv",
        csv_bytes(
            native,
            ["source", "participant", "at_utc", "metric", "value", "unit", "room"],
        ),
    )
    atomic_private_write(
        root / "stress-native.csv",
        csv_bytes(
            stresses,
            [
                "participant",
                "source_date",
                "graph",
                "segment",
                "point",
                "native_time_label",
                "displayed_score",
                "position_x",
                "position_y",
                "at_utc",
                "time_alignment",
                "captured_at",
            ],
        ),
    )
    atomic_private_write(
        root / "whoop-context.json", json.dumps(summaries, ensure_ascii=False).encode()
    )
    manifest["files"] = {
        name: {
            "sha256": hashlib.sha256((root / name).read_bytes()).hexdigest(),
            "bytes": (root / name).stat().st_size,
        }
        for name in (
            "series-6s.csv",
            "native-observations.csv",
            "stress-native.csv",
            "whoop-context.json",
        )
    }
    atomic_private_write(
        root / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2).encode(),
    )
    return manifest
