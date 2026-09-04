from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from time import sleep
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from health_agent.db import session_scope
from health_agent.models import DEFAULT_PROFILE_ID, Profile
from health_agent.whoop.auth_service import (
    AuthorizedWhoopAccount,
    publish_whoop_authorization,
)
from health_agent.whoop.client import (
    PROFILE_PATH,
    RECOVERY_PATH,
    WhoopAuthorizationRequired,
    WhoopClient,
    WhoopRateLimitDeferred,
)
from health_agent.whoop.models import (
    WhoopConnection,
    WhoopRawRecord,
    WhoopRecovery,
    WhoopSyncRun,
)
from health_agent.whoop.oauth import WHOOP_SCOPES, WhoopOAuth
from health_agent.whoop.repository import register_authorized_connection
from health_agent.whoop.status import get_whoop_status
from health_agent.whoop.sync import WhoopSyncReport, sync_whoop
from health_agent.whoop.tokens import TokenStore, TokenStoreError, WhoopToken


class FakeWhoopClient:
    def __init__(
        self, *, recovery_score: int = 44, fail_path: str | None = None
    ) -> None:
        self.recovery_score = recovery_score
        self.fail_path = fail_path
        self.starts: list[datetime | None] = []

    @contextmanager
    def operation(self) -> Iterator[None]:
        yield

    def recover_token(self, committed_generation: UUID | None) -> None:
        return

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
            WHOOP_SCOPES,
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
    assert status.auth_status == "reauth_required"


def test_status_reports_insufficient_scopes(session: Session, tmp_path: Path) -> None:
    connect(session)
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

    assert status.token_status == "insufficient_scopes"
    assert status.auth_status == "reauth_required"


def test_corrupt_initial_token_records_safe_reauth_audit(
    session: Session, tmp_path: Path
) -> None:
    connect(session)
    tokens = TokenStore(tmp_path / "tokens")
    token_path = tokens.save(
        str(DEFAULT_PROFILE_ID),
        "main",
        WhoopToken(
            "access-secret",
            "refresh-secret",
            datetime.now(UTC) + timedelta(hours=1),
            WHOOP_SCOPES,
        ),
    )
    token_path.write_text("corrupt-access-secret", encoding="utf-8")
    oauth = WhoopOAuth(
        "client-id",
        "client-secret",
        "http://127.0.0.1:8765/callback",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("OAuth request was not expected")
            )
        ),
    )
    client = WhoopClient(
        oauth,
        tokens,
        str(DEFAULT_PROFILE_ID),
        "main",
        http_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: pytest.fail("API request was not expected")
            )
        ),
    )

    report = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        client,
        full=True,
    )

    assert report.status == "failed"
    assert report.safe_error_code == "reauth_required"
    assert session.scalar(select(WhoopSyncRun.status)) == "failed"


def test_operation_lock_failure_records_safe_reauth_audit(session: Session) -> None:
    connect(session)

    class UnavailableTokenStorageClient(FakeWhoopClient):
        @contextmanager
        def operation(self) -> Iterator[None]:
            raise WhoopAuthorizationRequired(
                "WHOOP authorization storage is unavailable"
            )
            yield  # pragma: no cover - required for the contextmanager protocol

    report = sync_whoop(
        session,
        DEFAULT_PROFILE_ID,
        "main",
        UnavailableTokenStorageClient(),
        full=True,
    )

    assert report.status == "failed"
    assert report.safe_error_code == "reauth_required"
    assert session.scalar(select(WhoopSyncRun.status)) == "failed"


def test_status_survives_operation_lock_failure(
    session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connect(session)
    tokens = TokenStore(tmp_path / "tokens")

    def fail_directory(path: Path) -> None:
        raise TokenStoreError("synthetic unavailable token storage")

    monkeypatch.setattr(tokens, "_secure_directory", fail_directory)

    status = get_whoop_status(
        session, tokens, DEFAULT_PROFILE_ID, str(DEFAULT_PROFILE_ID), "main"
    )

    assert status.token_status == "unreadable"
    assert status.auth_status == "reauth_required"


def test_invalid_profile_identity_records_safe_failed_audit(session: Session) -> None:
    connect(session)

    class InvalidIdentityClient(FakeWhoopClient):
        def get_object(self, path: str) -> dict[str, Any]:
            if path == PROFILE_PATH:
                return {"user_id": "not-an-integer"}
            return super().get_object(path)

    report = sync_whoop(
        session, DEFAULT_PROFILE_ID, "main", InvalidIdentityClient(), full=True
    )

    assert report.status == "failed"
    assert report.safe_error_code == "sync_failed"
    assert session.scalar(select(WhoopSyncRun.status)) == "failed"


def test_long_rate_limit_records_deferred_retry_at(session: Session) -> None:
    connect(session)
    retry_at = datetime(2026, 9, 5, 12, tzinfo=UTC)

    class DeferredClient(FakeWhoopClient):
        def get_object(self, path: str) -> dict[str, Any]:
            raise WhoopRateLimitDeferred(retry_at)

    report = sync_whoop(
        session, DEFAULT_PROFILE_ID, "main", DeferredClient(), full=True
    )
    connection = session.scalar(select(WhoopConnection))
    run = session.scalar(select(WhoopSyncRun))

    assert report.status == "deferred"
    assert report.retry_at == retry_at
    assert connection is not None and connection.retry_at == retry_at
    assert run is not None and run.status == "deferred" and run.retry_at == retry_at


def test_concurrent_sync_and_reauthorization_do_not_deadlock(
    clean_database: Engine, tmp_path: Path
) -> None:
    with session_scope(clean_database) as session:
        connect(session)
    tokens = TokenStore(tmp_path / "tokens")
    tokens.save(
        str(DEFAULT_PROFILE_ID),
        "main",
        WhoopToken(
            "old-access",
            "old-refresh",
            datetime.now(UTC) + timedelta(hours=1),
            WHOOP_SCOPES,
        ),
    )
    sync_started = Event()
    release_sync = Event()
    errors: list[BaseException] = []

    class BlockingClient(FakeWhoopClient):
        @contextmanager
        def operation(self) -> Iterator[None]:
            with tokens.operation(str(DEFAULT_PROFILE_ID), "main"):
                yield

        def recover_token(self, committed_generation: UUID | None) -> None:
            tokens.recover(str(DEFAULT_PROFILE_ID), "main", committed_generation)

        def get_object(self, path: str) -> dict[str, Any]:
            if path == PROFILE_PATH:
                sync_started.set()
                if not release_sync.wait(timeout=3):
                    raise RuntimeError("test sync release timed out")
            return super().get_object(path)

    def run_sync() -> None:
        try:
            with session_scope(clean_database) as session:
                sync_whoop(
                    session,
                    DEFAULT_PROFILE_ID,
                    "main",
                    BlockingClient(),
                    full=True,
                )
        except Exception as error:  # noqa: BLE001 - assert thread failures in parent
            errors.append(error)

    def run_auth() -> None:
        try:
            publish_whoop_authorization(
                lambda: session_scope(clean_database),
                tokens,
                DEFAULT_PROFILE_ID,
                str(DEFAULT_PROFILE_ID),
                "main",
                AuthorizedWhoopAccount(
                    10129,
                    WHOOP_SCOPES,
                    WhoopToken(
                        "new-access",
                        "new-refresh",
                        datetime.now(UTC) + timedelta(hours=1),
                        WHOOP_SCOPES,
                    ),
                ),
            )
        except Exception as error:  # noqa: BLE001 - assert thread failures in parent
            errors.append(error)

    sync_thread = Thread(target=run_sync)
    auth_thread = Thread(target=run_auth)
    sync_thread.start()
    assert sync_started.wait(timeout=3)
    auth_thread.start()
    sleep(0.05)
    release_sync.set()
    sync_thread.join(timeout=5)
    auth_thread.join(timeout=5)

    assert not sync_thread.is_alive()
    assert not auth_thread.is_alive()
    assert errors == []
    assert tokens.load(str(DEFAULT_PROFILE_ID), "main").access_token == "new-access"  # type: ignore[union-attr]


def _summary(report: WhoopSyncReport) -> tuple[str, int, int, int, int]:
    return (
        report.status,
        report.raw_created,
        report.normalized_created,
        report.normalized_updated,
        report.unchanged,
    )
