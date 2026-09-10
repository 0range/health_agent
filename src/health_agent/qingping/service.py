"""Private connection state and recoverable, idempotent room measurements."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, Field, model_validator
from sqlalchemy import func, select

from health_agent.automation.storage import atomic_private_write, require_private_file
from health_agent.db import session_scope
from health_agent.pilot.storage import PilotRecord, PilotStore
from health_agent.qingping.client import QingpingClient, QingpingError

KIND = "room_measurement"
FIELDS = {
    "temperature": "°C",
    "humidity": "%",
    "co2": "ppm",
    "pm25": "µg/m³",
    "pm10": "µg/m³",
    "noise": "dB",
    "tvoc_index": "index",
    "battery": "%",
}


class Placement(BaseModel):
    since: AwareDatetime
    room: str = Field(min_length=1, max_length=100)


class Connection(BaseModel):
    profile_id: UUID
    mac: str = Field(pattern=r"^[0-9A-Fa-f]{12}$")
    start_at: AwareDatetime
    placements: list[Placement] = Field(min_length=1)

    @model_validator(mode="after")
    def ordered(self) -> Connection:
        times = [item.since for item in self.placements]
        if times != sorted(set(times)) or times[0] > self.start_at:
            raise ValueError("invalid_room_timeline")
        return self

    def room_at(self, at: datetime) -> str:
        return next(
            (p.room for p in reversed(self.placements) if p.since <= at), "unknown"
        )

    def save(self, path: Path) -> None:
        atomic_private_write(path, self.model_dump_json(indent=2).encode())

    @classmethod
    def load(cls, path: Path) -> Connection:
        return cls.model_validate_json(
            require_private_file(path.absolute()).read_text()
        )


def credentials(path: Path) -> tuple[str, str]:
    payload = json.loads(require_private_file(path.absolute()).read_text())
    values = payload.get("app_key"), payload.get("app_secret")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise QingpingError("qingping_credentials_invalid")
    return str(values[0]), str(values[1])


def observed_at(raw: dict[str, Any]) -> datetime:
    try:
        value = raw["timestamp"]["value"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (float, int))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise ValueError
        return datetime.fromtimestamp(value, UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        raise QingpingError("qingping_invalid_timestamp") from None


def metrics(raw: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for name, unit in FIELDS.items():
        item = raw.get(name)
        if isinstance(item, dict) and name != "battery" and item.get("status", 0) != 0:
            continue
        value = item.get("value") if isinstance(item, dict) else None
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        ):
            result[name] = {"value": value, "unit": unit}
    return result


class QingpingService:
    def __init__(
        self, store: PilotStore, connection: Connection, state_path: Path
    ) -> None:
        self.store, self.connection, self.state_path = store, connection, state_path

    def _state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        payload = json.loads(
            require_private_file(self.state_path.absolute()).read_text()
        )
        if (
            payload.get("profile_id") != str(self.connection.profile_id)
            or payload.get("mac") != self.connection.mac
        ):
            raise QingpingError("qingping_state_identity_mismatch")
        return dict(payload)

    def _save_state(self, state: dict[str, Any]) -> None:
        atomic_private_write(
            self.state_path,
            json.dumps(
                {
                    **state,
                    "profile_id": str(self.connection.profile_id),
                    "mac": self.connection.mac,
                }
            ).encode(),
        )

    def _query(self):
        return select(PilotRecord).where(
            PilotRecord.profile_id == self.connection.profile_id,
            PilotRecord.domain == "shared",
            PilotRecord.kind == KIND,
            PilotRecord.payload["device_mac"].astext == self.connection.mac,
        )

    def reconcile_rooms(self) -> None:
        # Repair an interrupted move without loading the entire minute archive.
        with session_scope(self.store.engine) as session:
            for index, placement in enumerate(self.connection.placements):
                query = self._query().where(
                    PilotRecord.at >= placement.since,
                    PilotRecord.payload["room"].astext.is_distinct_from(placement.room),
                )
                if index + 1 < len(self.connection.placements):
                    query = query.where(
                        PilotRecord.at < self.connection.placements[index + 1].since
                    )
                rows = session.scalars(query)
                for row in rows:
                    room = self.connection.room_at(row.at)
                    if row.payload.get("room") != room:
                        row.payload = {**row.payload, "room": room}

    def sync(
        self, client: QingpingClient, now: datetime | None = None
    ) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        self.reconcile_rooms()
        state = self._state()
        try:
            devices = [
                d
                for d in client.devices()
                if d.get("info", {}).get("mac", "").upper()
                == self.connection.mac.upper()
            ]
            if len(devices) != 1:
                raise QingpingError("qingping_device_not_found")
            device = devices[0]
            cursor = datetime.fromisoformat(
                state.get("cursor", self.connection.start_at.isoformat())
            )
            if cursor.tzinfo is None or cursor > now:
                raise QingpingError("qingping_invalid_cursor")
            reconciled = state.get("history_reconciled_at")
            reconcile = reconciled is None or now - datetime.fromisoformat(
                reconciled
            ) >= timedelta(hours=1)
            overlap = timedelta(days=1) if reconcile else timedelta(minutes=10)
            start = max(self.connection.start_at, cursor - overlap)
            target = min(now, cursor + timedelta(days=7))
            processed = 0
            while start < target:
                end = min(start + timedelta(days=1), target)
                rows = client.history(
                    self.connection.mac, int(start.timestamp()), int(end.timestamp())
                )
                for raw in rows:
                    at = observed_at(raw)
                    if not start <= at <= end:
                        raise QingpingError("qingping_history_outside_window")
                    self.store.put(
                        self.connection.profile_id,
                        "shared",
                        KIND,
                        f"qingping:{self.connection.mac}:{at.isoformat()}",
                        {
                            "provider": "qingping",
                            "device_mac": self.connection.mac,
                            "product": device.get("info", {}).get("product", {}),
                            "device_metadata": {
                                "firmware": device.get("info", {}).get("version"),
                                "settings": device.get("info", {}).get("setting", {}),
                                "observed_at": now.isoformat(),
                            },
                            "room": self.connection.room_at(at),
                            "metrics": metrics(raw),
                            "raw": raw,
                        },
                        at=at,
                    )
                    processed += 1
                cursor = max(cursor, end)
                state.update(cursor=cursor.isoformat())
                self._save_state(state)
                start = end
            interval = (
                device.get("info", {}).get("setting", {}).get("report_interval", 900)
            )
            state.update(
                last_success=now.isoformat(),
                last_error=None,
                report_interval=interval,
                offline=device.get("info", {}).get("status", {}).get("offline"),
                caught_up=cursor >= now,
            )
            if reconcile:
                state["history_reconciled_at"] = now.isoformat()
            self._save_state(state)
            return {"status": "synced", "processed": processed, **self.status(now)}
        except QingpingError as error:
            state["last_error"] = str(error)
            self._save_state(state)
            raise

    def status(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        state = self._state()
        with session_scope(self.store.engine) as session:
            latest = session.scalar(
                self._query().order_by(PilotRecord.at.desc()).limit(1)
            )
            count = session.scalar(
                select(func.count()).select_from(self._query().subquery())
            )
            timestamp = latest.at if latest is not None else None
            try:
                threshold = max(300, 3 * int(state.get("report_interval", 900)))
            except (ValueError, TypeError):
                threshold = 2700
            return {
                "samples": count,
                "last_success": state.get("last_success"),
                "last_error": state.get("last_error"),
                "caught_up": state.get("caught_up", False),
                "last_measurement": timestamp.isoformat() if timestamp else None,
                "stale": timestamp is None
                or (now - timestamp).total_seconds() > threshold,
                "offline": state.get("offline"),
                "report_interval_seconds": state.get("report_interval"),
                "room": self.connection.room_at(now),
                "measurement_room": self.connection.room_at(timestamp)
                if timestamp
                else None,
                "metrics": latest.payload.get("metrics", {})
                if latest is not None
                else {},
            }
