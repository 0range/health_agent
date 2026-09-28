from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.pilot.health_insights_whoop import read_whoop
from health_agent.whoop.normalize import normalize_whoop
from health_agent.whoop.repository import (
    register_authorized_connection,
    store_normalized_record,
)

NOW = datetime(2026, 9, 28, 12, tzinfo=UTC)


def save(session, connection, kind, data):
    store_normalized_record(session, connection, normalize_whoop(kind, data), data, NOW)


def observation(session, connection, identifier, day, *, nap=False, state="SCORED", hrv=45, calibrating=False):
    end = datetime.combine(day, datetime.min.time(), UTC) + timedelta(hours=4)
    save(session, connection, "sleep", {
        "id": str(identifier), "user_id": connection.external_user_id,
        "start": (end-timedelta(hours=8)).isoformat(), "end": end.isoformat(),
        "nap": nap, "score_state": state,
        "score": {"stage_summary": {"total_light_sleep_time_milli": 8*3600000,
                                    "total_slow_wave_sleep_time_milli": 0,"total_rem_sleep_time_milli": 0}},
    })
    save(session, connection, "recovery", {
        "cycle_id": identifier, "sleep_id": str(identifier), "user_id": connection.external_user_id,
        "score_state": "SCORED", "score": {"user_calibrating": calibrating,
            "hrv_rmssd_milli": hrv,"resting_heart_rate": 53,"recovery_score": 75},
    })


def test_reader_scopes_profiles_accounts_score_states_and_preserves_undated_body(clean_database):
    other = uuid4()
    with session_scope(clean_database) as session:
        session.add(Profile(id=other,name="Other"));session.flush()
        own=register_authorized_connection(session,DEFAULT_PROFILE_ID,"main",101,("read:sleep",))
        second=register_authorized_connection(session,other,"main",101,("read:sleep",))
        old=register_authorized_connection(session,DEFAULT_PROFILE_ID,"old",202,("read:sleep",))
        own.last_success_at=NOW
        old.last_success_at=NOW-timedelta(days=30)
        observation(session,own,1,date(2026,9,27))
        observation(session,second,1,date(2026,9,27),hrv=999)
        observation(session,old,1,date(2026,9,27),hrv=888)
        observation(session,own,2,date(2026,9,26),calibrating=True)
        observation(session,own,3,date(2026,9,25),nap=True)
        observation(session,own,4,date(2026,9,24),state="PENDING_SCORE")
        observation(session,own,5,date(2026,9,29))
        save(session,own,"body",{"height_meter":1.83,"weight_kilogram":76.7,"max_heart_rate":192})
    report=read_whoop(clean_database,DEFAULT_PROFILE_ID,date(2026,9,1),date(2026,9,28),NOW)
    assert len(report["records"])==2
    assert report["records"][0]["hrv_ms"]==45
    assert report["records"][1]["hrv_ms"] is None
    assert report["records"][0]["sleep_hours"]==8
    assert report["body_weight_kg_without_measurement_date"]==76.7
    assert report["body_weight_used_in_trend"] is False
    assert report["multiple_accounts"] is True
    assert read_whoop(clean_database,uuid4(),date(2026,9,1),date(2026,9,28),NOW)["status"]=="not_connected"
