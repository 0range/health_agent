"""Immutable, deliberately small data transfer objects for the local panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """A local profile identity safe to present in the management panel."""

    id: UUID
    name: str

    def to_dict(self) -> dict[str, str]:
        return {"id": str(self.id), "name": self.name}


@dataclass(frozen=True, slots=True)
class ConnectorCard:
    """A safe connector state with no credential or health-record fields."""

    connector: str
    status: str
    detail: str
    last_success_at: datetime | None = None
    error_code: str | None = None
    account_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "connector": self.connector,
            "status": self.status,
            "detail": self.detail,
            "last_success_at": (
                self.last_success_at.isoformat() if self.last_success_at else None
            ),
            "error_code": self.error_code,
            "account_ids": list(self.account_ids),
        }


@dataclass(frozen=True, slots=True)
class PanelDestination:
    """A safe local destination or an explicit unavailable placeholder."""

    key: str
    label: str
    url: str | None
    unavailable_text: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "key": self.key,
            "label": self.label,
            "url": self.url,
            "unavailable_text": self.unavailable_text,
        }


@dataclass(frozen=True, slots=True)
class ProfilePanel:
    """All safe panel data selected for one profile."""

    profile: ProfileSummary
    connectors: tuple[ConnectorCard, ...]
    drive_folder_ids: tuple[str, ...] = ()
    destinations: tuple[PanelDestination, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_dict(),
            "connectors": [connector.to_dict() for connector in self.connectors],
            "drive_folder_ids": list(self.drive_folder_ids),
            "destinations": [
                destination.to_dict() for destination in self.destinations
            ],
        }


@dataclass(frozen=True, slots=True)
class DataCoverage:
    """Dates and workflow counts safe for an operational status screen."""

    status: str
    latest_whoop_date: date | None = None
    latest_lab_collected_date: date | None = None
    latest_lab_issued_date: date | None = None
    latest_received_at: datetime | None = None
    pending_extraction_count: int | None = None
    needs_review_count: int | None = None
    verified_count: int | None = None
    whoop_status: str | None = None
    labs_status: str | None = None
    extraction_status: str | None = None
    coros_status: str | None = None
    apple_status: str | None = None
    extraction_queued_count: int | None = None
    extraction_running_count: int | None = None
    extraction_waiting_cloud_count: int | None = None
    extraction_cloud_in_flight_count: int | None = None
    extraction_needs_attention_count: int | None = None
    coros_activity_count: int | None = None
    coros_first_date: date | None = None
    coros_latest_date: date | None = None
    coros_last_sync_at: datetime | None = None
    apple_weight_count: int | None = None
    apple_weight_first_date: date | None = None
    apple_weight_latest_date: date | None = None
    apple_workout_candidate_count: int | None = None
    apple_workout_first_date: date | None = None
    apple_workout_latest_date: date | None = None
    apple_imported_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class BotStatus:
    """Safe local state for one Telegram bot process."""

    domain: str
    label: str
    status: str
    configured: bool
    bound: bool
    last_poll_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class HealthcheckProfile:
    """Connector state and local data coverage for exactly one profile."""

    panel: ProfilePanel
    coverage: DataCoverage
    bots: tuple[BotStatus, ...] = ()


@dataclass(frozen=True, slots=True)
class HealthcheckSnapshot:
    """One immutable, read-only healthcheck result."""

    checked_at: datetime
    profiles: tuple[HealthcheckProfile, ...]
