"""Strict profile and OAuth configuration for Google Sheets."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from health_agent.google_drive.config import validate_profile_id
from health_agent.google_sheets.types import SheetsAccountIdentity

SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SHEETS_SCOPES = frozenset((SHEETS_SCOPE, DRIVE_FILE_SCOPE))
WORKBOOK_SCHEMA_VERSION = "health-agent-sheets-v1"

_OPAQUE_ID = re.compile(r"[A-Za-z0-9_-]{8,300}")


def _clean_identity(
    permission_id: str | None, email: str | None
) -> tuple[str | None, str | None]:
    if permission_id is None and email is None:
        return None, None
    if not isinstance(permission_id, str) or not isinstance(email, str):
        raise ValueError(  # noqa: TRY004 - a paired option is semantically incomplete
            "Google account binding must contain both ID and email"
        )
    permission_id = permission_id.strip()
    email = email.strip().casefold()
    if not permission_id or any(char.isspace() for char in permission_id):
        raise ValueError("Google account permission ID is invalid")
    if "@" not in email or any(char.isspace() for char in email):
        raise ValueError("Google account email is invalid")
    return permission_id, email


@dataclass(frozen=True, slots=True)
class SheetsProfile:
    profile_id: str
    expected_permission_id: str | None = None
    expected_email: str | None = None
    spreadsheet_id: str | None = None
    spreadsheet_url: str | None = None
    workbook_token: str | None = None
    projection_initialized: bool = False
    creation_state: Literal["not_started", "in_flight", "unknown", "created"] = (
        "not_started"
    )

    @classmethod
    def create(
        cls,
        profile_id: str,
        *,
        expected_permission_id: str | None = None,
        expected_email: str | None = None,
    ) -> SheetsProfile:
        permission_id, email = _clean_identity(expected_permission_id, expected_email)
        return cls(validate_profile_id(profile_id), permission_id, email)

    def with_account(self, identity: SheetsAccountIdentity) -> SheetsProfile:
        permission_id, email = _clean_identity(identity.permission_id, identity.email)
        if self.expected_permission_id not in {
            None,
            permission_id,
        } or self.expected_email not in {
            None,
            email,
        }:
            raise ValueError("Sheets authorization belongs to another Google account")
        return SheetsProfile(
            self.profile_id,
            permission_id,
            email,
            self.spreadsheet_id,
            self.spreadsheet_url,
            self.workbook_token,
            self.projection_initialized,
            self.creation_state,
        )

    def with_workbook(
        self, spreadsheet_id: str, spreadsheet_url: str, workbook_token: str
    ) -> SheetsProfile:
        if _OPAQUE_ID.fullmatch(spreadsheet_id.strip()) is None:
            raise ValueError("spreadsheet ID is invalid")
        parsed = urlsplit(spreadsheet_url.strip())
        if (
            parsed.scheme != "https"
            or parsed.hostname != "docs.google.com"
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("spreadsheet URL is invalid")
        if _OPAQUE_ID.fullmatch(workbook_token.strip()) is None:
            raise ValueError("workbook binding token is invalid")
        if (
            self.workbook_token is not None
            and self.workbook_token != workbook_token.strip()
        ):
            raise ValueError("created workbook token differs from creation fence")
        return SheetsProfile(
            self.profile_id,
            self.expected_permission_id,
            self.expected_email,
            spreadsheet_id.strip(),
            spreadsheet_url.strip(),
            workbook_token.strip(),
            self.projection_initialized,
            "created",
        )

    def with_creation_started(self, workbook_token: str) -> SheetsProfile:
        if self.spreadsheet_id is not None or self.creation_state != "not_started":
            raise ValueError("workbook creation is already fenced")
        if _OPAQUE_ID.fullmatch(workbook_token.strip()) is None:
            raise ValueError("workbook binding token is invalid")
        return SheetsProfile(
            self.profile_id,
            self.expected_permission_id,
            self.expected_email,
            workbook_token=workbook_token.strip(),
            creation_state="in_flight",
        )

    def with_unknown_creation(self) -> SheetsProfile:
        if self.spreadsheet_id is not None or self.creation_state != "in_flight":
            raise ValueError("workbook creation is not in flight")
        return SheetsProfile(
            self.profile_id,
            self.expected_permission_id,
            self.expected_email,
            workbook_token=self.workbook_token,
            creation_state="unknown",
        )

    def reset_creation_fence(self) -> SheetsProfile:
        if self.spreadsheet_id is not None or self.creation_state not in {
            "in_flight",
            "unknown",
        }:
            raise ValueError("workbook creation is not awaiting recovery")
        return SheetsProfile(
            self.profile_id,
            self.expected_permission_id,
            self.expected_email,
        )

    def with_initialized_projection(self) -> SheetsProfile:
        if self.spreadsheet_id is None:
            raise ValueError("cannot initialize a missing workbook")
        return SheetsProfile(
            self.profile_id,
            self.expected_permission_id,
            self.expected_email,
            self.spreadsheet_id,
            self.spreadsheet_url,
            self.workbook_token,
            True,
            self.creation_state,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> SheetsProfile:
        profile = cls.create(
            str(payload["profile_id"]),
            expected_permission_id=payload.get("expected_permission_id"),
            expected_email=payload.get("expected_email"),
        )
        workbook_values = (
            payload.get("spreadsheet_id"),
            payload.get("spreadsheet_url"),
            payload.get("workbook_token"),
        )
        initialized = payload.get("projection_initialized", False)
        if not isinstance(initialized, bool):
            raise TypeError("stored projection initialization flag is invalid")
        creation_state = payload.get(
            "creation_state",
            "created" if workbook_values[0] is not None else "not_started",
        )
        if creation_state not in {"not_started", "in_flight", "unknown", "created"}:
            raise ValueError("stored workbook creation state is invalid")
        if workbook_values[:2] == (None, None):
            if initialized:
                raise ValueError("missing workbook cannot be initialized")
            token = workbook_values[2]
            if creation_state == "not_started" and token is None:
                return profile
            if creation_state not in {"in_flight", "unknown"} or not isinstance(
                token, str
            ):
                raise ValueError("stored workbook creation fence is invalid")
            if _OPAQUE_ID.fullmatch(token) is None:
                raise ValueError("stored workbook binding token is invalid")
            return SheetsProfile(
                profile.profile_id,
                profile.expected_permission_id,
                profile.expected_email,
                workbook_token=token,
                creation_state=creation_state,  # type: ignore[arg-type]
            )
        if not all(isinstance(value, str) for value in workbook_values):
            raise ValueError("stored workbook binding is incomplete")
        if creation_state != "created":
            raise ValueError("stored workbook creation state is invalid")
        configured = profile.with_workbook(*workbook_values)  # type: ignore[arg-type]
        return configured.with_initialized_projection() if initialized else configured
