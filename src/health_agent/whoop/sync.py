from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from health_agent.whoop.client import (
    BODY_PATH,
    CYCLE_PATH,
    PROFILE_PATH,
    RECOVERY_PATH,
    SLEEP_PATH,
    WORKOUT_PATH,
    WhoopApiError,
    WhoopAuthorizationRequired,
)
from health_agent.whoop.models import WhoopSyncRun
from health_agent.whoop.normalize import WhoopNormalizationError, normalize_whoop
from health_agent.whoop.repository import (
    StoredRecord,
    WhoopRepositoryError,
    get_connection,
    store_normalized_record,
)

INCREMENTAL_OVERLAP = timedelta(days=7)


class WhoopDataClient(Protocol):
    def get_object(self, path: str) -> dict[str, Any]: ...

    def iter_collection_pages(
        self,
        path: str,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[tuple[dict[str, Any], ...]]: ...


@dataclass(frozen=True, slots=True)
class WhoopSyncReport:
    status: str
    mode: str
    requested_from: datetime | None
    raw_created: int
    normalized_created: int
    normalized_updated: int
    unchanged: int
    safe_error_code: str | None = None


def sync_whoop(
    session: Session,
    profile_id: UUID,
    account_name: str,
    client: WhoopDataClient,
    *,
    full: bool = False,
    now: datetime | None = None,
) -> WhoopSyncReport:
    """Synchronize one profile/account; safe for CLI and future local UI calls."""
    sync_time = now or datetime.now(UTC)
    if sync_time.tzinfo is None:
        raise ValueError("WHOOP sync time must be timezone-aware")
    connection = get_connection(session, profile_id, account_name)
    requested_from = (
        None
        if full or connection.last_success_at is None
        else connection.last_success_at - INCREMENTAL_OVERLAP
    )
    mode = "backfill" if requested_from is None else "incremental"
    connection.last_attempt_at = sync_time
    run = WhoopSyncRun(
        profile_id=profile_id,
        connection_id=connection.id,
        mode=mode,
        status="running",
        requested_from=requested_from,
        started_at=sync_time,
    )
    session.add(run)
    session.flush()
    counts = StoredRecord()

    try:
        with session.begin_nested():
            profile_payload = client.get_object(PROFILE_PATH)
            profile_record = normalize_whoop("profile", profile_payload)
            remote_user_id = int(profile_record.external_id)
            if (
                connection.external_user_id is not None
                and connection.external_user_id != remote_user_id
            ):
                raise WhoopRepositoryError(
                    "WHOOP token belongs to a different account than this connection"
                )
            connection.external_user_id = remote_user_id
            counts = _add(
                counts,
                store_normalized_record(
                    session, connection, profile_record, profile_payload, sync_time
                ),
            )

            body_payload = client.get_object(BODY_PATH)
            counts = _add(
                counts,
                store_normalized_record(
                    session,
                    connection,
                    normalize_whoop("body", body_payload),
                    body_payload,
                    sync_time,
                ),
            )

            resources = (
                ("cycle", CYCLE_PATH),
                ("recovery", RECOVERY_PATH),
                ("sleep", SLEEP_PATH),
                ("workout", WORKOUT_PATH),
            )
            for resource_kind, path in resources:
                for page in client.iter_collection_pages(
                    path, start=requested_from, end=sync_time
                ):
                    for payload in page:
                        counts = _add(
                            counts,
                            store_normalized_record(
                                session,
                                connection,
                                normalize_whoop(resource_kind, payload),
                                payload,
                                sync_time,
                            ),
                        )
    except WhoopAuthorizationRequired:
        return _fail_sync(
            run, connection, mode, requested_from, "reauth_required", sync_time
        )
    except (
        WhoopApiError,
        WhoopNormalizationError,
        WhoopRepositoryError,
        SQLAlchemyError,
    ):
        return _fail_sync(
            run, connection, mode, requested_from, "sync_failed", sync_time
        )

    connection.last_success_at = sync_time
    connection.last_error_code = None
    connection.auth_status = "connected"
    run.status = "succeeded"
    run.completed_at = sync_time
    _set_counts(run, counts)
    return WhoopSyncReport(
        status="succeeded",
        mode=mode,
        requested_from=requested_from,
        raw_created=counts.raw_created,
        normalized_created=counts.normalized_created,
        normalized_updated=counts.normalized_updated,
        unchanged=counts.unchanged,
    )


def _fail_sync(
    run: WhoopSyncRun,
    connection: Any,
    mode: str,
    requested_from: datetime | None,
    error_code: str,
    completed_at: datetime,
) -> WhoopSyncReport:
    run.status = "failed"
    run.safe_error_code = error_code
    run.completed_at = completed_at
    connection.last_error_code = error_code
    if error_code == "reauth_required":
        connection.auth_status = "reauth_required"
    return WhoopSyncReport(
        status="failed",
        mode=mode,
        requested_from=requested_from,
        raw_created=0,
        normalized_created=0,
        normalized_updated=0,
        unchanged=0,
        safe_error_code=error_code,
    )


def _add(left: StoredRecord, right: StoredRecord) -> StoredRecord:
    return StoredRecord(
        raw_created=left.raw_created + right.raw_created,
        normalized_created=left.normalized_created + right.normalized_created,
        normalized_updated=left.normalized_updated + right.normalized_updated,
        unchanged=left.unchanged + right.unchanged,
    )


def _set_counts(run: WhoopSyncRun, counts: StoredRecord) -> None:
    run.raw_created = counts.raw_created
    run.normalized_created = counts.normalized_created
    run.normalized_updated = counts.normalized_updated
    run.unchanged = counts.unchanged
