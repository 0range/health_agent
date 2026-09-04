from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.whoop.client import RECOVERY_PATH
from health_agent.whoop.models import (
    WhoopConnection,
    WhoopRawRecord,
    WhoopRecovery,
    WhoopSyncRun,
)
from health_agent.whoop.repository import register_authorized_connection
from health_agent.whoop.status import get_whoop_status
from health_agent.whoop.sync import WhoopSyncReport, sync_whoop
from health_agent.whoop.tokens import TokenStore, WhoopToken


class FakeWhoopClient:
    def __init__(
        self, *, recovery_score: int = 44, fail_path: str | None = None
    ) -> None:
        self.recovery_score = recovery_score
        self.fail_path = fail_path
        self.starts: list[datetime | None] = []

    def get_object(self, path: str) -> dict[str, Any]:
        if path.endswith("profile/basic"):
            return {
                "user_id": 10129,
                "email": "person@example.test",
                "first_name": "Test",
                "last_name": "Person",
            }
        return {"height_meter": 1.82, "weight_kilogram": 80.5, "max_heart_rate": 190}

    def iter_collection_pages(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[tuple[dict[str, Any], ...]]:
        self.starts.append(start)
        if path == self.fail_path:
            from health_agent.whoop.client import WhoopApiError

            raise WhoopApiError("synthetic failure")
        yield (self._payload(path),)

    def _payload(self, path: str) -> dict[str, Any]:
        common = {
            "user_id": 10129,
            "updated_at": "2026-09-04T08:00:00Z",
            "score_state": "SCORED",
        }
        if path.endswith("/cycle"):
            return {
                **common,
                "id": 93845,
                "start": "2026-09-03T22:00:00Z",
                "end": "2026-09-04T08:00:00Z",
                "timezone_offset": "+03:00",
                "score": {"strain": 5.2, "average_heart_rate": 68},
            }
        if path.endswith("/recovery"):
            return {
                **common,
                "cycle_id": 93845,
                "sleep_id": "sleep-1",
                "score": {
                    "recovery_score": self.recovery_score,
                    "resting_heart_rate": 60,
                    "hrv_rmssd_milli": 42.5,
                    "spo2_percentage": 97,
                    "skin_temp_celsius": 33.4,
                },
            }
        if path.endswith("/sleep"):
            return {
                **common,
                "id": "sleep-1",
                "cycle_id": 93845,
                "start": "2026-09-03T22:00:00Z",
                "end": "2026-09-04T06:00:00Z",
                "timezone_offset": "+03:00",
                "nap": False,
                "score": {"sleep_performance_percentage": 90},
            }
        return {
            **common,
            "id": "workout-1",
            "start": "2026-09-04T06:30:00Z",
            "end": "2026-09-04T07:30:00Z",
            "timezone_offset": "+03:00",
            "sport_id": 1,
            "sport_name": "running",
            "score": {"strain": 8.1, "average_heart_rate": 140},
        }


def connect(session: Session, profile_id: UUID = DEFAULT_PROFILE_ID) -> WhoopConnection:
    return register_authorized_connection(
        session,
        profile_id,
        "main",
        10129,
        (
            "offline",
            "read:profile",
            "read:body_measurement",
            "read:cycles",
            "read:recovery",
            "read:sleep",
            "read:workout",
        ),
    )


def test_repeated_full_sync_has_no_duplicates_and_changed_revision_updates(
    session: Session,
) -> None:
    connect(session)
    first_time = datetime(2026, 9, 4, 9, tzinfo=UTC)

    first = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        FakeWhoopClient(),
        full=True,
        now=first_time,
    )
    second = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        FakeWhoopClient(),
        full=True,
        now=first_time,
    )
    changed = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        FakeWhoopClient(recovery_score=55),
        full=True,
        now=first_time,
    )

    assert _summary(first) == ("succeeded", 6, 6, 0, 0)
    assert _summary(second) == ("succeeded", 0, 0, 0, 6)
    assert _summary(changed) == ("succeeded", 1, 0, 1, 5)
    assert session.scalar(select(func.count()).select_from(WhoopRecovery)) == 1
    assert session.scalar(select(WhoopRecovery.recovery_score)) == 55
    assert session.scalar(select(func.count()).select_from(WhoopRawRecord)) == 7


def test_incremental_sync_overlaps_last_seven_days(session: Session) -> None:
    connection = connect(session)
    connection.last_success_at = datetime(2026, 9, 4, 9, tzinfo=UTC)
    client = FakeWhoopClient()

    report = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        client,
        now=datetime(2026, 9, 5, 9, tzinfo=UTC),
    )

    assert report.mode == "incremental"
    assert client.starts == [datetime(2026, 8, 28, 9, tzinfo=UTC)] * 4


def test_partial_failure_rolls_back_data_but_keeps_safe_status(
    session: Session,
) -> None:
    connect(session)

    report = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        FakeWhoopClient(fail_path=RECOVERY_PATH),
        full=True,
        now=datetime(2026, 9, 4, 9, tzinfo=UTC),
    )

    assert report.status == "failed"
    assert report.safe_error_code == "sync_failed"
    assert session.scalar(select(func.count()).select_from(WhoopRawRecord)) == 0
    assert session.scalar(select(WhoopSyncRun.status)) == "failed"
    connection = session.scalar(select(WhoopConnection))
    assert connection is not None
    assert connection.last_success_at is None


def test_two_profile_syncs_never_mix_rows(session: Session) -> None:
    second_profile = Profile(id=uuid4(), name="Second person")
    session.add(second_profile)
    session.flush()
    connect(session, DEFAULT_PROFILE_ID)
    connect(session, second_profile.id)

    sync_whoop(session, DEFAULT_PROFILE_ID, "main", FakeWhoopClient(), full=True)
    sync_whoop(session, second_profile.id, "main", FakeWhoopClient(), full=True)

    grouped = session.execute(
        select(WhoopRecovery.profile_id, func.count()).group_by(
            WhoopRecovery.profile_id
        )
    ).all()
    assert {row[0]: row[1] for row in grouped} == {
        DEFAULT_PROFILE_ID: 1,
        second_profile.id: 1,
    }


def test_status_is_safe_and_scoped_to_selected_profile(
    session: Session, tmp_path: Path
) -> None:
    connect(session)
    sync_whoop(session, DEFAULT_PROFILE_ID, "main", FakeWhoopClient(), full=True)
    tokens = TokenStore(tmp_path / "tokens")
    tokens.save(
        str(DEFAULT_PROFILE_ID),
        "main",
        WhoopToken(
            "access",
            "refresh",
            datetime.now(UTC) + timedelta(hours=1),
            ("offline",),
        ),
    )

    status = get_whoop_status(
        session, tokens, DEFAULT_PROFILE_ID, str(DEFAULT_PROFILE_ID), "main"
    )
    missing_id = uuid4()
    missing = get_whoop_status(session, tokens, missing_id, str(missing_id), "main")

    assert status.configured is True
    assert status.token_status == "ready"
    assert status.weight_available is True
    assert (
        status.cycle_count,
        status.recovery_count,
        status.sleep_count,
        status.workout_count,
    ) == (
        1,
        1,
        1,
        1,
    )
    assert missing.configured is False
    assert missing.token_status == "missing"


def test_status_reports_unreadable_token_without_exposing_contents(
    session: Session, tmp_path: Path
) -> None:
    connect(session)
    tokens = TokenStore(tmp_path / "tokens")
    path = tokens.save(
        str(DEFAULT_PROFILE_ID),
        "main",
        WhoopToken(
            "access",
            "refresh",
            datetime.now(UTC) + timedelta(hours=1),
            ("offline",),
        ),
    )
    path.write_text("not-json", encoding="utf-8")

    status = get_whoop_status(
        session, tokens, DEFAULT_PROFILE_ID, str(DEFAULT_PROFILE_ID), "main"
    )

    assert status.token_status == "unreadable"


def _summary(report: WhoopSyncReport) -> tuple[str, int, int, int, int]:
    return (
        report.status,
        report.raw_created,
        report.normalized_created,
        report.normalized_updated,
        report.unchanged,
    )
