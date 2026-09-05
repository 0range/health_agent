from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from health_agent.google_sheets.api import GoogleSheetsGateway, safe_sheets_error_code
from health_agent.google_sheets.decisions import ReviewGridError
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
    assert next(iter(body["requests"][0])) == "updateSheetProperties"
    assert next(iter(body["requests"][1])) == "updateCells"


def test_review_read_preserves_typed_literals_and_sparse_offsets() -> None:
    sheets = MagicMock()
    spreadsheets = sheets.spreadsheets.return_value
    spreadsheets.get.side_effect = (
        _request(
            {
                "sheets": [
                    {
                        "properties": {
                            "title": "Needs review",
                            "sheetId": 17,
                            "gridProperties": {"rowCount": 1000, "columnCount": 26},
                        }
                    }
                ]
            }
        ),
        _request(
            {
                "sheets": [
                    {
                        "properties": {"title": "Needs review", "sheetId": 17},
                        "data": [
                            {
                                "startRow": 1,
                                "startColumn": 2,
                                "rowData": [
                                    {
                                        "values": [
                                            {
                                                "userEnteredValue": {
                                                    "stringValue": "=literal"
                                                }
                                            },
                                            {},
                                            {"userEnteredValue": {"numberValue": 12.5}},
                                            {"userEnteredValue": {"boolValue": True}},
                                        ]
                                    },
                                    {},
                                    {
                                        "values": [
                                            {
                                                "userEnteredValue": {
                                                    "stringValue": "tail"
                                                }
                                            }
                                        ]
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        ),
    )
    gateway = GoogleSheetsGateway(sheets, MagicMock())
    assert gateway.read_review_rows("spreadsheet_123") == (
        (),
        (None, None, "=literal", None, 12.5, True),
        (),
        (None, None, "tail"),
    )
    native_read = spreadsheets.get.call_args_list[1].kwargs
    assert native_read["ranges"] == ["'Needs review'!A1:Z1000"]
    assert native_read["includeGridData"] is True


def test_review_read_rejects_actual_formula_with_same_text_as_literal() -> None:
    sheets = MagicMock()
    spreadsheets = sheets.spreadsheets.return_value
    spreadsheets.get.side_effect = (
        _request(
            {
                "sheets": [
                    {
                        "properties": {
                            "title": "Needs review",
                            "sheetId": 17,
                            "gridProperties": {"rowCount": 1000, "columnCount": 26},
                        }
                    }
                ]
            }
        ),
        _request(
            {
                "sheets": [
                    {
                        "properties": {"title": "Needs review", "sheetId": 17},
                        "data": [
                            {
                                "rowData": [
                                    {
                                        "values": [
                                            {
                                                "userEnteredValue": {
                                                    "formulaValue": "=literal"
                                                }
                                            }
                                        ]
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        ),
    )
    gateway = GoogleSheetsGateway(sheets, MagicMock())
    with pytest.raises(ReviewGridError, match="formulas"):
        gateway.read_review_rows("spreadsheet_123")


def test_binding_read_requests_formula_source_not_calculated_values() -> None:
    sheets = MagicMock()
    values = sheets.spreadsheets.return_value.values.return_value
    values.get.return_value = _request(
        {
            "values": [
                ["profile_id", "00000000-0000-0000-0000-000000000001"],
                ["schema_version", "health-agent-sheets-v1"],
                ["workbook_token", "token_12345678"],
                ["projection_initialized", "true"],
            ]
        }
    )
    gateway = GoogleSheetsGateway(sheets, MagicMock())
    assert gateway.read_binding("spreadsheet_123").projection_initialized is True
    assert values.get.call_args.kwargs["valueRenderOption"] == "FORMULA"


def test_replace_expands_grid_before_writing_large_projection() -> None:
    sheets = MagicMock()
    spreadsheets = sheets.spreadsheets.return_value
    spreadsheets.get.return_value = _request(
        {
            "sheets": [
                {
                    "properties": {
                        "title": title,
                        "sheetId": index,
                        "gridProperties": {"rowCount": 10, "columnCount": 2},
                    }
                }
                for index, title in enumerate(
                    ("Lab history", "Needs review", "Sources", "_HealthAgent"), 1
                )
            ]
        }
    )
    spreadsheets.batchUpdate.return_value = _request({})
    gateway = GoogleSheetsGateway(sheets, MagicMock())
    large = ManagedSheet("Lab history", ("A",), tuple((str(i),) for i in range(1100)))
    projection = WorkbookProjection(
        WorkbookBinding(
            "00000000-0000-0000-0000-000000000001",
            "health-agent-sheets-v1",
            "token_12345678",
        ),
        (
            large,
            ManagedSheet("Needs review", ("A",), ()),
            ManagedSheet("Sources", ("A",), ()),
        ),
    )
    gateway.replace_managed_tabs("spreadsheet_123", projection)
    resize = spreadsheets.batchUpdate.call_args.kwargs["body"]["requests"][0][
        "updateSheetProperties"
    ]
    assert resize["properties"]["gridProperties"]["rowCount"] == 1101


def test_safe_error_does_not_leak_google_body() -> None:
    response = MagicMock(status=403, reason="private remote text")
    error = HttpError(response, b'{"error":{"message":"private remote text"}}')
    code = safe_sheets_error_code(error)
    assert code == "permission_denied"
    assert "private remote text" not in code
