from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopConnection,
    WhoopCycle,
    WhoopRecovery,
    WhoopSleep,
    WhoopWorkout,
)
from health_agent.whoop.oauth import WHOOP_SCOPES
from health_agent.whoop.tokens import TokenStore, TokenStoreError


@dataclass(frozen=True, slots=True)
class WhoopStatus:
    configured: bool
    auth_status: str
    token_status: str
    last_success_at: datetime | None
    retry_at: datetime | None
    last_error_code: str | None
    weight_available: bool
    cycle_count: int
    recovery_count: int
    sleep_count: int
    workout_count: int


def get_whoop_status(
    session: Session,
    token_store: TokenStore,
    profile_id: UUID,
    profile_key: str,
    account_name: str,
) -> WhoopStatus:
    """Return non-sensitive status for the CLI and future local management UI."""
    try:
        with token_store.operation(profile_key, account_name):
            return _status_under_operation(
                session, token_store, profile_id, profile_key, account_name
            )
    except TokenStoreError:
        return _status_from_database(
            session, profile_id, account_name, token_status="unreadable"
        )


def _status_under_operation(
    session: Session,
    token_store: TokenStore,
    profile_id: UUID,
    profile_key: str,
    account_name: str,
) -> WhoopStatus:
    connection = _connection(session, profile_id, account_name)
    try:
        token_store.recover(
            profile_key,
            account_name,
            connection.token_generation if connection else None,
        )
        token = token_store.load(profile_key, account_name)
        if token is None:
            token_status = "missing"
        elif token.expired:
            token_status = "expired"
        elif set(WHOOP_SCOPES).difference(token.scopes):
            token_status = "insufficient_scopes"
        else:
            token_status = "ready"
    except TokenStoreError:
        token_status = "unreadable"
    return _status_from_database(
        session,
        profile_id,
        account_name,
        token_status=token_status,
        connection=connection,
    )


def _connection(
    session: Session, profile_id: UUID, account_name: str
) -> WhoopConnection | None:
    return session.scalar(
        select(WhoopConnection).where(
            WhoopConnection.profile_id == profile_id,
            WhoopConnection.account_name == account_name,
        )
    )


def _status_from_database(
    session: Session,
    profile_id: UUID,
    account_name: str,
    *,
    token_status: str,
    connection: WhoopConnection | None = None,
) -> WhoopStatus:
    if connection is None:
        connection = _connection(session, profile_id, account_name)
    if connection is None:
        return WhoopStatus(
            False,
            "not_connected",
            token_status,
            None,
            None,
            None,
            False,
            0,
            0,
            0,
            0,
        )

    def count(model: type[Any]) -> int:
        value: int | None = session.scalar(
            select(func.count())
            .select_from(model)
            .where(
                model.profile_id == profile_id,
                model.connection_id == connection.id,
            )
        )
        return value or 0

    weight_available = (
        session.scalar(
            select(WhoopBodyCurrent.weight_kilogram).where(
                WhoopBodyCurrent.profile_id == profile_id,
                WhoopBodyCurrent.connection_id == connection.id,
            )
        )
        is not None
    )
    effective_auth_status = connection.auth_status
    if token_status in {"missing", "unreadable", "insufficient_scopes"}:
        effective_auth_status = "reauth_required"
    return WhoopStatus(
        configured=True,
        auth_status=effective_auth_status,
        token_status=token_status,
        last_success_at=connection.last_success_at,
        retry_at=connection.retry_at,
        last_error_code=connection.last_error_code,
        weight_available=weight_available,
        cycle_count=count(WhoopCycle),
        recovery_count=count(WhoopRecovery),
        sleep_count=count(WhoopSleep),
        workout_count=count(WhoopWorkout),
    )
