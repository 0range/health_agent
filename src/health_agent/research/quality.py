"""Daily local evidence of completeness; a successful HTTP request is insufficient."""

from __future__ import annotations

import json
import math
import shutil
import subprocess
from collections import Counter
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import Engine, select, text

from health_agent.automation.storage import atomic_private_write
from health_agent.db import session_scope
from health_agent.pilot.storage import PilotRecord
from health_agent.qingping.service import FIELDS, Connection
from health_agent.research.calendar import STUDY_START, ZONE, bounds, recent_days
from health_agent.whoop.models import WhoopConnection, WhoopRecovery, WhoopSleep

GIB = 1024**3


def coverage(times: list[datetime], start: datetime, end: datetime) -> dict[str, Any]:
    seconds = (end - start).total_seconds()
    if seconds <= 0:
        raise ValueError("invalid_coverage_window")
    points = sorted({t for t in times if start <= t < end})
    bins = {int((t - start).total_seconds() // 6) for t in points}
    gaps: list[dict[str, Any]] = [
        {"from": a.isoformat(), "to": b.isoformat(), "seconds": (b - a).total_seconds()}
        for a, b in zip([start, *points], [*points, end], strict=True)
        if (b - a).total_seconds() > 12
    ]
    largest = max((g["seconds"] for g in gaps), default=0)
    percentage = 100 * len(bins) / math.ceil(seconds / 6)
    return {
        "samples": len(points),
        "occupied_6s_bins": len(bins),
        "expected_6s_bins": math.ceil(seconds / 6),
        "coverage_percent": round(percentage, 3),
        "first": points[0].isoformat() if points else None,
        "last": points[-1].isoformat() if points else None,
        "largest_gap_seconds": largest,
        "gaps_over_12_seconds": gaps,
        "status": "ok" if percentage >= 99 and largest <= 60 else "gaps",
    }


def capacity(engine: Engine, root: Path, container: str) -> dict[str, Any]:
    """Conservative estimate from measured compressed payloads plus overhead.

    Includes 31 days, two WHOOPs, four daily resources revisited hourly, three
    times payload bytes for indexes/row overhead/growth, and a 10-GiB floor.
    This is a forecast, not a reservation or provider retention guarantee.
    """
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text("""
            select kind, payload->>'resource' as resource,
                   max(pg_column_size(payload)) as bytes
            from pilot_records where kind in ('room_measurement', 'whoop_detail')
            group by kind, payload->>'resource'
        """)
            )
            .mappings()
            .all()
        )
        db_bytes = connection.scalar(
            text("select pg_database_size(current_database())")
        )
    sizes = {(r["kind"], r["resource"]): int(r["bytes"]) for r in rows}
    air = max(2000, sizes.get(("room_measurement", None), 0)) * 14400
    whoop = (
        sum(
            max(floor, sizes.get(("whoop_detail", kind), 0))
            for kind, floor in (
                ("heart_rate", 120000),
                ("stress", 250000),
                ("sleep_stages", 10000),
            )
        )
        * 4
        * 24
        * 2
    )
    forecast = max(10 * GIB, 31 * (air + whoop) * 3)
    host_free = shutil.disk_usage(root).free
    database_free = None
    try:
        result = subprocess.run(
            ["docker", "exec", container, "df", "-Pk", "/var/lib/postgresql"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        database_free = int(result.stdout.splitlines()[-1].split()[3]) * 1024
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        pass
    enough = (
        database_free is not None
        and database_free >= forecast + 5 * GIB
        and host_free >= forecast + 5 * GIB
    )
    return {
        "status": "ok" if enough else "attention",
        "database_bytes": db_bytes,
        "host_free_bytes": host_free,
        "database_filesystem_free_bytes": database_free,
        "forecast_31_days_two_whoops_bytes": forecast,
        "additional_free_reserve_bytes": 5 * GIB,
        "method": "measured_max_compressed_payloads_x3_minimum_10_GiB",
    }


class QualityService:
    def __init__(
        self,
        engine: Engine,
        sensor: Connection,
        root: Path,
        detail_root: Path,
        sensor_root: Path,
        container: str,
    ) -> None:
        self.engine, self.sensor, self.root = engine, sensor, root
        self.detail_root, self.sensor_root, self.container = (
            detail_root,
            sensor_root,
            container,
        )

    def day(self, day: date, profile_id: UUID, user_id: int) -> dict[str, Any]:
        start, end = bounds(day)
        with session_scope(self.engine) as session:
            air = session.execute(
                select(
                    PilotRecord.at,
                    PilotRecord.payload["metrics"],
                    PilotRecord.payload["room"].astext,
                ).where(
                    PilotRecord.profile_id == self.sensor.profile_id,
                    PilotRecord.domain == "shared",
                    PilotRecord.kind == "room_measurement",
                    PilotRecord.payload["device_mac"].astext == self.sensor.mac,
                    PilotRecord.at >= start,
                    PilotRecord.at < end,
                )
            ).all()
            # Discard older UTC-day snapshots; exact study timezone and account
            # must match. Sort revisions by retrieval time, never UUID ordering.
            details = session.scalars(
                select(PilotRecord).where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.domain == "shared",
                    PilotRecord.kind == "whoop_detail",
                    PilotRecord.payload["external_user_id"].astext == str(user_id),
                    PilotRecord.payload["calendar_timezone"].astext == str(ZONE),
                    PilotRecord.payload["logical_id"].astext == day.isoformat(),
                    PilotRecord.at == start,
                )
            ).all()
            latest: dict[str, dict[str, Any]] = {}
            for row in sorted(
                details,
                key=lambda r: r.payload.get(
                    "last_seen_at", r.payload.get("captured_at", "")
                ),
            ):
                latest[row.payload["resource"]] = row.payload
            sleeps = session.scalars(
                select(WhoopSleep).where(
                    WhoopSleep.profile_id == profile_id,
                    WhoopSleep.external_user_id == user_id,
                    WhoopSleep.end_at >= start,
                    WhoopSleep.end_at < end,
                    WhoopSleep.is_nap.is_(False),
                )
            ).all()
            sleep_ids = [s.external_id for s in sleeps]
            recoveries = session.scalars(
                select(WhoopRecovery).where(
                    WhoopRecovery.profile_id == profile_id,
                    WhoopRecovery.external_user_id == user_id,
                    WhoopRecovery.sleep_id.in_(sleep_ids),
                )
            ).all()
            stages = session.scalar(
                select(PilotRecord.id)
                .where(
                    PilotRecord.profile_id == profile_id,
                    PilotRecord.kind == "whoop_detail",
                    PilotRecord.payload["external_user_id"].astext == str(user_id),
                    PilotRecord.payload["resource"].astext == "sleep_stages",
                    PilotRecord.payload["logical_id"].astext.in_(sleep_ids),
                )
                .limit(1)
            )
            recovery_fields = {
                field: sum(getattr(r, field) is not None for r in recoveries)
                for field in (
                    "hrv_rmssd_milli",
                    "resting_heart_rate",
                    "spo2_percentage",
                    "skin_temp_celsius",
                )
            }
        air_report = coverage([r[0] for r in air], start, end)
        air_report["rooms"] = dict(Counter(r[2] for r in air))
        air_report["fields"] = {
            field: coverage([r[0] for r in air if field in (r[1] or {})], start, end)[
                "coverage_percent"
            ]
            for field in FIELDS
            if field != "battery"
        }
        if any(value < 99 for value in air_report["fields"].values()):
            air_report["status"] = "gaps"
        hr = latest.get("heart_rate", {}).get("raw", {}).get("values", [])
        hr_times = []
        for sample in hr:
            try:
                value, timestamp = sample["data"], sample["time"]
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value <= 0
                ):
                    continue
                hr_times.append(datetime.fromtimestamp(timestamp / 1000, UTC))
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
        hr_report = coverage(hr_times, start, end)
        stress_raw = latest.get("stress", {}).get("raw", {})
        stress_points = sum(
            len(segment.get("points", []))
            for plot in stress_raw.get("stress_graph", {})
            .get("graph", {})
            .get("plots", [])
            if plot.get("type") == "LINE_PLOT"
            for segment in plot.get("plot", {}).get("segments", [])
        )
        stress_present = stress_points > 0
        nightly_ok = (
            bool(sleeps) and bool(stages) and recovery_fields["hrv_rmssd_milli"] > 0
        )
        status = (
            "ok"
            if (
                air_report["status"] == hr_report["status"] == "ok"
                and stress_present
                and nightly_ok
            )
            else "attention"
        )
        if day < STUDY_START:
            status = "before_study"
        elif day == STUDY_START:
            status = "setup_day"  # Device paired during the day, not at midnight.
        return {
            "date": day.isoformat(),
            "timezone": str(ZONE),
            "status": status,
            "profile_id": str(profile_id),
            "start_utc": start.isoformat(),
            "end_utc": end.isoformat(),
            "air": air_report,
            "heart_rate": hr_report,
            "stress": {
                "status": "native_graph_available_unaligned"
                if stress_present
                else "missing",
                "native_graph_points": stress_points,
            },
            "nightly": {
                "sleeps": len(sleep_ids),
                "sleep_stages_available": bool(stages),
                "recovery_field_counts": recovery_fields,
                "status": "ok" if nightly_ok else "missing",
            },
            "interpretation": "99_percent_6s_bins_and_no_gap_over_60s; stress_has_no_verified_UTC_sample_times",
        }

    def run(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        reports = []
        with session_scope(self.engine) as session:
            accounts = session.execute(
                select(
                    WhoopConnection.profile_id,
                    WhoopConnection.external_user_id,
                    WhoopConnection.last_success_at,
                    WhoopConnection.auth_status,
                    WhoopConnection.last_error_code,
                ).where(
                    WhoopConnection.profile_id == self.sensor.profile_id,
                    WhoopConnection.external_user_id.is_not(None),
                )
            ).all()
        days = recent_days(now) if now.astimezone(ZONE).hour >= 10 else []
        for profile_id, user_id, _, _, _ in accounts:
            for day in days:
                report = {
                    **self.day(day, profile_id, user_id),
                    "checked_at": now.isoformat(),
                }
                atomic_private_write(
                    self.root / "daily" / str(profile_id) / f"{day.isoformat()}.json",
                    json.dumps(report, ensure_ascii=False, indent=2).encode(),
                )
                reports.append(
                    {
                        "date": day.isoformat(),
                        "profile_id": str(profile_id),
                        "status": report["status"],
                    }
                )
        sources: dict[str, dict[str, Any]] = {}
        for name, folder, time_key, limit in (
            ("air", self.sensor_root, "last_success", 600),
            ("whoop_detail", self.detail_root, "last_attempt", 7200),
        ):
            try:
                state = json.loads((folder / "state.json").read_text())
                at = datetime.fromisoformat(state[time_key])
                error_path = folder / "last_error.json"
                error = (
                    json.loads(error_path.read_text()) if error_path.exists() else {}
                )
                # A successful sync replaces this error file; a failed renewal
                # must be visible even when yesterday's stored data is complete.
                healthy = (
                    0 <= (now - at).total_seconds() <= limit
                    and not state.get("last_error")
                    and not state.get("errors")
                    and not error
                )
                sources[name] = {
                    "status": "ok" if healthy else "attention",
                    "last_sync": at.isoformat(),
                }
            except (OSError, ValueError, KeyError, TypeError):
                sources[name] = {"status": "unknown"}
        sources["whoop_public"] = {
            "status": "ok"
            if accounts
            and all(
                at is not None
                and 0 <= (now - at).total_seconds() < 8 * 3600
                and auth == "connected"
                and error is None
                for _, _, at, auth, error in accounts
            )
            else "attention"
        }
        with session_scope(self.engine) as session:
            air_last = session.scalar(
                select(PilotRecord.at)
                .where(
                    PilotRecord.profile_id == self.sensor.profile_id,
                    PilotRecord.kind == "room_measurement",
                    PilotRecord.payload["device_mac"].astext == self.sensor.mac,
                )
                .order_by(PilotRecord.at.desc())
                .limit(1)
            )
            hr_last = session.scalar(
                select(PilotRecord.payload)
                .where(
                    PilotRecord.profile_id == self.sensor.profile_id,
                    PilotRecord.kind == "whoop_detail",
                    PilotRecord.payload["resource"].astext == "heart_rate",
                    PilotRecord.payload["calendar_timezone"].astext == str(ZONE),
                )
                .order_by(
                    PilotRecord.at.desc(),
                    PilotRecord.payload["last_seen_at"].astext.desc().nulls_last(),
                    PilotRecord.payload["captured_at"].astext.desc(),
                )
                .limit(1)
            )
        hr_times = [
            s.get("time", 0) for s in (hr_last or {}).get("raw", {}).get("values", [])
        ]
        hr_latest = (
            datetime.fromtimestamp(max(hr_times) / 1000, UTC) if hr_times else None
        )
        for source, measurement_at, limit in (
            ("air", air_last, 600),
            ("whoop_detail", hr_latest, 7200),
        ):
            sources[source]["last_measurement"] = (
                measurement_at.isoformat() if measurement_at else None
            )
            if (
                measurement_at is None
                or not 0 <= (now - measurement_at).total_seconds() <= limit
            ):
                sources[source]["status"] = "attention"
        disk = capacity(self.engine, self.root, self.container)
        healthy = (
            bool(accounts)
            and disk["status"] == "ok"
            and all(s["status"] == "ok" for s in sources.values())
            and all(r["status"] in {"ok", "setup_day"} for r in reports)
        )
        result = {
            "checked_at": now.isoformat(),
            "timezone": str(ZONE),
            "study_start": STUDY_START.isoformat(),
            "status": "ok" if healthy else "attention",
            "daily_reports": reports,
            "sources": sources,
            "storage": disk,
            "daily_review_after": "10:00 Europe/Moscow",
            "configured_whoops": len(accounts),
        }
        atomic_private_write(
            self.root / "status.json", json.dumps(result, indent=2).encode()
        )
        return result
