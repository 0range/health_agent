from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from health_agent.whoop.models import (
    WhoopBodyCurrent,
    WhoopConnection,
    WhoopCycle,
    WhoopProfileCurrent,
    WhoopRawRecord,
    WhoopRecovery,
    WhoopSleep,
    WhoopWorkout,
)
from health_agent.whoop.normalize import NormalizedWhoopRecord


class WhoopRepositoryError(RuntimeError):
    """A safe storage/identity error."""


@dataclass(frozen=True, slots=True)
class StoredRecord:
    raw_created: int = 0
    normalized_created: int = 0
    normalized_updated: int = 0
    unchanged: int = 0


_HISTORY_MODELS = {
    "cycle": WhoopCycle,
    "recovery": WhoopRecovery,
    "sleep": WhoopSleep,
    "workout": WhoopWorkout,
}


def register_authorized_connection(
    session: Session,
    profile_id: UUID,
    account_name: str,
    external_user_id: int,
    granted_scopes: tuple[str, ...],
) -> WhoopConnection:
    connection = session.scalar(
        select(WhoopConnection).where(
            WhoopConnection.profile_id == profile_id,
            WhoopConnection.account_name == account_name,
        )
    )
    existing_identity = session.scalar(
        select(WhoopConnection).where(
            WhoopConnection.profile_id == profile_id,
            WhoopConnection.external_user_id == external_user_id,
        )
    )
    if existing_identity is not None and existing_identity is not connection:
        raise WhoopRepositoryError(
            "This WHOOP account is already connected to the selected profile"
        )
    if connection is None:
        connection = WhoopConnection(
            profile_id=profile_id,
            account_name=account_name,
            external_user_id=external_user_id,
            granted_scopes=list(granted_scopes),
            auth_status="connected",
        )
        session.add(connection)
    else:
        if (
            connection.external_user_id is not None
            and connection.external_user_id != external_user_id
        ):
            raise WhoopRepositoryError(
                "The account name belongs to a different WHOOP identity"
            )
        connection.external_user_id = external_user_id
        connection.granted_scopes = list(granted_scopes)
        connection.auth_status = "connected"
        connection.last_error_code = None
    session.flush()
    return connection


def get_connection(
    session: Session, profile_id: UUID, account_name: str
) -> WhoopConnection:
    connection = session.scalar(
        select(WhoopConnection).where(
            WhoopConnection.profile_id == profile_id,
            WhoopConnection.account_name == account_name,
        )
    )
    if connection is None:
        raise WhoopRepositoryError("WHOOP account is not connected for this profile")
    return connection


def store_normalized_record(
    session: Session,
    connection: WhoopConnection,
    normalized: NormalizedWhoopRecord,
    raw_payload: dict[str, Any],
    fetched_at: datetime,
) -> StoredRecord:
    raw = session.scalar(
        select(WhoopRawRecord).where(
            WhoopRawRecord.profile_id == connection.profile_id,
            WhoopRawRecord.connection_id == connection.id,
            WhoopRawRecord.resource_kind == normalized.resource_kind,
            WhoopRawRecord.external_id == normalized.external_id,
            WhoopRawRecord.payload_sha256 == normalized.payload_hash,
        )
    )
    raw_created = 0
    if raw is None:
        raw = WhoopRawRecord(
            profile_id=connection.profile_id,
            connection_id=connection.id,
            resource_kind=normalized.resource_kind,
            external_id=normalized.external_id,
            payload_sha256=normalized.payload_hash,
            payload=raw_payload,
            source_updated_at=normalized.source_updated_at,
            fetched_at=fetched_at,
        )
        session.add(raw)
        session.flush()
        raw_created = 1

    model, identity = _model_and_identity(normalized, connection)
    current = session.scalar(select(model).where(*identity))
    if current is None:
        values = {
            "profile_id": connection.profile_id,
            "connection_id": connection.id,
            "raw_record_id": raw.id,
            **normalized.values,
        }
        if normalized.resource_kind in _HISTORY_MODELS:
            values["external_id"] = normalized.external_id
            values["source_updated_at"] = normalized.source_updated_at
        if normalized.resource_kind == "profile":
            values["fetched_at"] = fetched_at
        if normalized.resource_kind == "body":
            values["observed_at"] = fetched_at
        session.add(model(**values))
        session.flush()
        return StoredRecord(raw_created=raw_created, normalized_created=1)

    current_raw_id = current.raw_record_id
    if normalized.resource_kind == "profile":
        current.fetched_at = fetched_at
    if normalized.resource_kind == "body":
        current.observed_at = fetched_at
    if current_raw_id == raw.id:
        return StoredRecord(raw_created=raw_created, unchanged=1)

    for key, value in normalized.values.items():
        setattr(current, key, value)
    current.raw_record_id = raw.id
    if normalized.resource_kind in _HISTORY_MODELS:
        current.source_updated_at = normalized.source_updated_at
    session.flush()
    return StoredRecord(raw_created=raw_created, normalized_updated=1)


def _model_and_identity(
    normalized: NormalizedWhoopRecord, connection: WhoopConnection
) -> tuple[type[Any], tuple[Any, ...]]:
    common = (
        lambda model: model.profile_id == connection.profile_id,
        lambda model: model.connection_id == connection.id,
    )
    if normalized.resource_kind == "profile":
        model: type[Any] = WhoopProfileCurrent
    elif normalized.resource_kind == "body":
        model = WhoopBodyCurrent
    else:
        try:
            model = _HISTORY_MODELS[normalized.resource_kind]
        except KeyError as error:
            raise WhoopRepositoryError(
                "Unsupported normalized WHOOP resource"
            ) from error
    identity = [condition(model) for condition in common]
    if normalized.resource_kind in _HISTORY_MODELS:
        identity.append(model.external_id == normalized.external_id)
    return model, tuple(identity)
