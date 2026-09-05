from __future__ import annotations

from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from health_agent.google_sheets.api import GoogleSheetsGateway, safe_sheets_error_code
from health_agent.google_sheets.types import (
    ManagedSheet,
    WorkbookBinding,
    WorkbookProjection,
)


def _request(value):
    request = MagicMock()
    request.execute.return_value = value
    return request


def test_replace_uses_one_atomic_batch_update() -> None:
    sheets = MagicMock()
    spreadsheets = sheets.spreadsheets.return_value
    spreadsheets.get.return_value = _request(
        {
            "sheets": [
                {"properties": {"title": title, "sheetId": index}}
                for index, title in enumerate(
                    ("Lab history", "Needs review", "Sources", "_HealthAgent"), 1
                )
            ]
        }
    )
    spreadsheets.batchUpdate.return_value = _request({})
    gateway = GoogleSheetsGateway(sheets, MagicMock())
    projection = WorkbookProjection(
        WorkbookBinding(
            "00000000-0000-0000-0000-000000000001",
            "health-agent-sheets-v1",
            "token_12345678",
        ),
        (
            ManagedSheet("Lab history", ("A",), (("one",),)),
            ManagedSheet("Needs review", ("Decision",), (("",),), (0,)),
            ManagedSheet("Sources", ("Source",), (("whoop",),)),
        ),
    )
    gateway.replace_managed_tabs("spreadsheet_123", projection)
    assert spreadsheets.batchUpdate.call_count == 1
    body = spreadsheets.batchUpdate.call_args.kwargs["body"]
    keys = {next(iter(request)) for request in body["requests"]}
    assert {"updateCells", "setDataValidation"} <= keys


def test_safe_error_does_not_leak_google_body() -> None:
    response = MagicMock(status=403, reason="private remote text")
    error = HttpError(response, b'{"error":{"message":"private remote text"}}')
    code = safe_sheets_error_code(error)
    assert code == "permission_denied"
    assert "private remote text" not in code
