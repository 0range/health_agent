"""Read bounded WHOOP observations for one wearer, never profile weight history."""

from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, and_, select

from health_agent.db import session_scope
from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopConnection,
    WhoopRecovery,
    WhoopSleep,
)

ZONE = ZoneInfo("Europe/Moscow")


def read_whoop(
    engine: Engine, profile: UUID, first: date, last: date, now: datetime,
) -> dict[str, Any]:
    with session_scope(engine) as session:
        connections = list(session.scalars(select(WhoopConnection).where(
            WhoopConnection.profile_id == profile,
        ).order_by(WhoopConnection.last_success_at.desc().nullslast())))
        if not connections:
            return {"status": "not_connected", "records": []}
        connection = connections[0]
        rows = session.execute(select(WhoopSleep, WhoopRecovery).outerjoin(
            WhoopRecovery, and_(
                WhoopRecovery.profile_id == WhoopSleep.profile_id,
                WhoopRecovery.connection_id == WhoopSleep.connection_id,
                WhoopRecovery.sleep_id == WhoopSleep.external_id,
            ),
        ).where(
            WhoopSleep.profile_id == profile,
            WhoopSleep.connection_id == connection.id,
            WhoopSleep.is_nap.is_(False),
            WhoopSleep.score_state == "SCORED",
            WhoopSleep.end_at >= datetime.combine(first, time.min, ZONE),
            WhoopSleep.end_at < datetime.combine(last + timedelta(days=1), time.min, ZONE),
            WhoopSleep.end_at <= now,
        ).order_by(WhoopSleep.end_at.desc()).limit(200)).all()
        records = []
        for sleep, recovery in rows:
            scored = recovery is not None and recovery.score_state == "SCORED" and recovery.user_calibrating is False
            record: dict[str, Any] = {
                "start_at": sleep.start_at.isoformat(),
                "end_at": sleep.end_at.isoformat() if sleep.end_at else None,
                "sleep_hours": sleep.total_sleep_milli / 3_600_000 if sleep.total_sleep_milli is not None else None,
                "sleep_efficiency": float(sleep.sleep_efficiency_percentage) if sleep.sleep_efficiency_percentage is not None else None,
            }
            for name, attribute in (("hrv_ms", "hrv_rmssd_milli"), ("resting_hr", "resting_heart_rate"), ("recovery", "recovery_score")):
                value = getattr(recovery, attribute) if scored else None
                record[name] = float(value) if value is not None else None
            records.append(record)
        body = session.scalar(select(WhoopBodyCurrent).where(
            WhoopBodyCurrent.profile_id == profile,
            WhoopBodyCurrent.connection_id == connection.id,
        ))
        return {
            "status": "ok" if connection.auth_status == "connected" and not connection.last_error_code else "attention",
            "sync_at": connection.last_success_at.isoformat() if connection.last_success_at else None,
            "records": records,
            "truncated": len(rows) == 200,
            "multiple_accounts": len(connections) > 1,
            "body_weight_kg_without_measurement_date": float(body.weight_kilogram) if body and body.weight_kilogram is not None else None,
            "body_weight_used_in_trend": False,
        }
