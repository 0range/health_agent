"""Immutable, deliberately small data transfer objects for the local panel."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
class ProfilePanel:
    """All safe panel data selected for one profile."""

    profile: ProfileSummary
    connectors: tuple[ConnectorCard, ...]
    drive_folder_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_dict(),
            "connectors": [connector.to_dict() for connector in self.connectors],
            "drive_folder_ids": list(self.drive_folder_ids),
        }
