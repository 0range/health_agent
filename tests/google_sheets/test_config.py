from __future__ import annotations

from uuid import uuid4

import pytest

from health_agent.google_sheets.config import SheetsProfile
from health_agent.google_sheets.types import SheetsAccountIdentity


def test_profile_validates_identity_and_workbook() -> None:
    profile_id = str(uuid4())
    profile = SheetsProfile.create(
        profile_id,
        expected_permission_id="permission-1",
        expected_email="Me@Example.com",
    )
    assert profile.expected_email == "me@example.com"
    assert (
        profile.with_account(SheetsAccountIdentity("permission-1", "me@example.com"))
        .with_workbook(
            "sheet_id-123",
            "https://docs.google.com/spreadsheets/d/sheet_id-123/edit",
            "token_12345678",
        )
        .spreadsheet_id
        == "sheet_id-123"
    )


def test_profile_refuses_partial_or_changed_identity() -> None:
    profile_id = str(uuid4())
    with pytest.raises(ValueError):
        SheetsProfile.create(profile_id, expected_permission_id="permission-1")
    profile = SheetsProfile.create(
        profile_id,
        expected_permission_id="permission-1",
        expected_email="me@example.com",
    )
    with pytest.raises(ValueError, match="another Google account"):
        profile.with_account(SheetsAccountIdentity("permission-2", "me@example.com"))
