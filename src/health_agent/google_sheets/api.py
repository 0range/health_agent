"""Small mockable adapter for the official Google Sheets and Drive APIs."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable
from typing import Any, cast

import httplib2  # type: ignore[import-untyped]
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]
from googleapiclient.discovery import Resource, build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]
from tenacity import (
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

from health_agent.google_sheets.decisions import ReviewGridError
from health_agent.google_sheets.types import (
    CreatedWorkbook,
    ManagedSheet,
    SheetsAccountIdentity,
    SheetValue,
    WorkbookBinding,
    WorkbookProjection,
)

_MANAGED_TITLES = ("Lab history", "Needs review", "Sources", "_HealthAgent")
_MAX_MANAGED_ROWS = 50_000
_MAX_MANAGED_CELLS = 500_000


def _column_name(column_count: int) -> str:
    name = ""
    remaining = column_count
    while remaining:
        remaining, offset = divmod(remaining - 1, 26)
        name = chr(ord("A") + offset) + name
    return name


def _native_cell_value(cell: object) -> tuple[bool, SheetValue]:
    if not isinstance(cell, dict):
        raise ReviewGridError("invalid native review cell")
    entered = cell.get("userEnteredValue")
    if entered is None:
        return False, None
    if not isinstance(entered, dict) or len(entered) != 1:
        raise ReviewGridError("invalid native review cell")
    kind, value = next(iter(entered.items()))
    if kind == "formulaValue":
        raise ReviewGridError("formulas are not allowed in the review sheet")
    if kind == "stringValue" and isinstance(value, str):
        return True, value
    if kind == "boolValue" and isinstance(value, bool):
        return True, value
    if (
        kind == "numberValue"
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return True, value
    raise ReviewGridError("invalid native review cell")


def _native_review_rows(
    payload: dict[str, Any], *, sheet_id: int, row_count: int, column_count: int
) -> tuple[tuple[SheetValue, ...], ...]:
    sheets = payload.get("sheets")
    if not isinstance(sheets, list) or len(sheets) != 1:
        raise ReviewGridError("invalid native review grid")
    sheet = sheets[0]
    if not isinstance(sheet, dict):
        raise ReviewGridError("invalid native review grid")
    properties = sheet.get("properties")
    if (
        not isinstance(properties, dict)
        or properties.get("title") != "Needs review"
        or properties.get("sheetId") != sheet_id
    ):
        raise ReviewGridError("invalid native review grid")
    data = sheet.get("data", [])
    if not isinstance(data, list):
        raise ReviewGridError("invalid native review grid")
    rows: dict[int, list[SheetValue]] = {}
    last_row = -1
    for grid in data:
        if not isinstance(grid, dict):
            raise ReviewGridError("invalid native review grid")
        start_row = grid.get("startRow", 0)
        start_column = grid.get("startColumn", 0)
        row_data = grid.get("rowData", [])
        if (
            not isinstance(start_row, int)
            or isinstance(start_row, bool)
            or not isinstance(start_column, int)
            or isinstance(start_column, bool)
            or start_row < 0
            or start_column < 0
            or not isinstance(row_data, list)
            or start_row + len(row_data) > row_count
        ):
            raise ReviewGridError("invalid native review grid")
        for row_offset, row in enumerate(row_data):
            row_index = start_row + row_offset
            last_row = max(last_row, row_index)
            if not isinstance(row, dict):
                raise ReviewGridError("invalid native review grid")
            cells = row.get("values", [])
            if not isinstance(cells, list) or start_column + len(cells) > column_count:
                raise ReviewGridError("invalid native review grid")
            if not cells:
                continue
            values = rows.setdefault(row_index, [])
            required_width = start_column + len(cells)
            values.extend([None] * (required_width - len(values)))
            for cell_offset, cell in enumerate(cells):
                present, value = _native_cell_value(cell)
                if present:
                    values[start_column + cell_offset] = value
    if last_row < 0:
        return ()
    return tuple(tuple(rows.get(row_index, ())) for row_index in range(last_row + 1))


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, socket.timeout)):
        return True
    if error.__class__.__module__.startswith("httplib2"):
        return True
    if not isinstance(error, HttpError):
        return False
    status = int(getattr(error.resp, "status", 0))
    if status == 429 or status >= 500:
        return True
    if status != 403:
        return False
    try:
        payload = json.loads(error.content.decode("utf-8"))
        reasons = {
            detail.get("reason")
            for detail in payload.get("error", {}).get("errors", [])
            if isinstance(detail, dict)
        }
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return bool(reasons & {"rateLimitExceeded", "userRateLimitExceeded"})


def _retry[T](call: Callable[[], T]) -> T:
    retryer = Retrying(
        stop=stop_after_attempt(5),
        wait=wait_random_exponential(multiplier=1, max=16),
        retry=retry_if_exception(_is_retryable),
        reraise=True,
    )
    return cast(T, retryer(call))


def _execute(request: Any) -> dict[str, Any]:
    return cast(dict[str, Any], _retry(lambda: request.execute(num_retries=0)))


def safe_sheets_error_code(error: BaseException) -> str:
    if isinstance(error, HttpError):
        status = int(getattr(error.resp, "status", 0))
        if status == 401:
            return "oauth_required"
        if status == 403:
            return "permission_denied"
        if status == 404:
            return "workbook_not_found"
        if status == 429:
            return "rate_limited"
        if status >= 500:
            return "google_unavailable"
    if _is_retryable(error):
        return "google_unavailable"
    return "sheets_request_failed"


def _extended_value(value: SheetValue) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, (int, float)):
        return {"numberValue": value}
    return {"stringValue": str(value)}


def _row_data(values: tuple[SheetValue, ...]) -> dict[str, Any]:
    return {
        "values": [
            (
                {"userEnteredValue": extended}
                if (extended := _extended_value(value))
                else {}
            )
            for value in values
        ]
    }


def _binding_rows(binding: WorkbookBinding) -> tuple[tuple[SheetValue, ...], ...]:
    return (
        ("profile_id", binding.profile_id),
        ("schema_version", binding.schema_version),
        ("workbook_token", binding.workbook_token),
        (
            "projection_initialized",
            "true" if binding.projection_initialized else "false",
        ),
    )


class GoogleSheetsGateway:
    def __init__(self, sheets: Resource, drive: Resource) -> None:
        self._sheets = sheets
        self._drive = drive

    @classmethod
    def from_credentials(
        cls, credentials: Credentials, *, timeout_seconds: int = 30
    ) -> GoogleSheetsGateway:
        transport = AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=timeout_seconds)
        )
        return cls(
            build("sheets", "v4", http=transport, cache_discovery=False),
            build("drive", "v3", http=transport, cache_discovery=False),
        )

    def account_identity(self) -> SheetsAccountIdentity:
        payload = _execute(
            self._drive.about().get(fields="user(permissionId,emailAddress)")
        )
        user = payload["user"]
        return SheetsAccountIdentity(
            permission_id=str(user["permissionId"]),
            email=str(user["emailAddress"]).casefold(),
        )

    def create_workbook(self, title: str, binding: WorkbookBinding) -> CreatedWorkbook:
        sheets: list[dict[str, Any]] = []
        for managed_title in _MANAGED_TITLES:
            sheet: dict[str, Any] = {
                "properties": {
                    "title": managed_title,
                    "hidden": managed_title == "_HealthAgent",
                    "gridProperties": {"frozenRowCount": 1},
                }
            }
            if managed_title == "_HealthAgent":
                sheet["data"] = [
                    {
                        "startRow": 0,
                        "startColumn": 0,
                        "rowData": [_row_data(row) for row in _binding_rows(binding)],
                    }
                ]
            sheets.append(sheet)
        # Workbook creation is not safely idempotent. Do not retry an ambiguous
        # transport failure and risk producing several private spreadsheets.
        payload = cast(
            dict[str, Any],
            self._sheets.spreadsheets()
            .create(
                body={"properties": {"title": title}, "sheets": sheets},
                fields="spreadsheetId,spreadsheetUrl",
            )
            .execute(num_retries=0),
        )
        spreadsheet_id = str(payload["spreadsheetId"])
        return CreatedWorkbook(
            spreadsheet_id,
            str(
                payload.get(
                    "spreadsheetUrl",
                    f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit",
                )
            ),
        )

    def read_binding(self, spreadsheet_id: str) -> WorkbookBinding:
        payload = _execute(
            self._sheets.spreadsheets()
            .values()
            .get(
                spreadsheetId=spreadsheet_id,
                range="'_HealthAgent'!A1:B4",
                valueRenderOption="FORMULA",
                dateTimeRenderOption="FORMATTED_STRING",
            )
        )
        rows = payload.get("values")
        if not isinstance(rows, list) or len(rows) != 4:
            raise ValueError("invalid workbook binding")
        values: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("invalid workbook binding")
            key, value = row
            if not isinstance(key, str) or not isinstance(value, str) or key in values:
                raise ValueError("invalid workbook binding")
            values[key] = value
        if set(values) != {
            "profile_id",
            "schema_version",
            "workbook_token",
            "projection_initialized",
        }:
            raise ValueError("invalid workbook binding")
        initialized = values["projection_initialized"]
        if initialized not in {"true", "false"}:
            raise ValueError("invalid workbook binding")
        return WorkbookBinding(
            values["profile_id"],
            values["schema_version"],
            values["workbook_token"],
            initialized == "true",
        )

    def read_review_rows(
        self, spreadsheet_id: str
    ) -> tuple[tuple[SheetValue, ...], ...]:
        metadata = _execute(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields=(
                    "sheets(properties(sheetId,title,"
                    "gridProperties(rowCount,columnCount)))"
                ),
            )
        )
        raw_sheets = metadata.get("sheets", [])
        matches = (
            [
                sheet.get("properties")
                for sheet in raw_sheets
                if isinstance(sheet, dict)
                and isinstance(sheet.get("properties"), dict)
                and sheet["properties"].get("title") == "Needs review"
            ]
            if isinstance(raw_sheets, list)
            else []
        )
        if len(matches) != 1:
            raise ReviewGridError("review sheet metadata mismatch")
        properties = matches[0]
        assert isinstance(properties, dict)
        grid = properties.get("gridProperties")
        sheet_id = properties.get("sheetId")
        if not isinstance(grid, dict) or not isinstance(sheet_id, int):
            raise ReviewGridError("review sheet metadata mismatch")
        row_count = grid.get("rowCount")
        column_count = grid.get("columnCount")
        if (
            not isinstance(row_count, int)
            or isinstance(row_count, bool)
            or not isinstance(column_count, int)
            or isinstance(column_count, bool)
            or row_count < 1
            or column_count < 1
            or row_count > _MAX_MANAGED_ROWS
            or row_count * column_count > _MAX_MANAGED_CELLS
        ):
            raise ReviewGridError("review sheet grid exceeds the v0.1 size limit")
        review_range = f"'Needs review'!A1:{_column_name(column_count)}{row_count}"
        payload = _execute(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                ranges=[review_range],
                includeGridData=True,
                fields=(
                    "sheets(properties(sheetId,title),"
                    "data(startRow,startColumn,rowData(values(userEnteredValue))))"
                ),
            )
        )
        return _native_review_rows(
            payload,
            sheet_id=sheet_id,
            row_count=row_count,
            column_count=column_count,
        )

    def replace_managed_tabs(
        self, spreadsheet_id: str, projection: WorkbookProjection
    ) -> None:
        metadata = _execute(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields=(
                    "sheets(properties(sheetId,title,hidden,"
                    "gridProperties(rowCount,columnCount)))"
                ),
            )
        )
        sheet_properties = {
            str(sheet["properties"]["title"]): sheet["properties"]
            for sheet in metadata.get("sheets", [])
        }
        if set(sheet_properties) != set(_MANAGED_TITLES):
            raise ValueError("managed workbook tabs changed")
        managed = (
            *projection.sheets,
            ManagedSheet("_HealthAgent", (), _binding_rows(projection.binding)),
        )
        requests: list[dict[str, Any]] = []
        for sheet in managed:
            rows = (() if not sheet.headers else (sheet.headers,)) + sheet.rows
            required_columns = max((len(row) for row in rows), default=1)
            if (
                len(rows) > _MAX_MANAGED_ROWS
                or len(rows) * required_columns > _MAX_MANAGED_CELLS
            ):
                raise ValueError("managed sheet projection exceeds the v0.1 size limit")
            properties = sheet_properties[sheet.title]
            sheet_id = int(properties["sheetId"])
            grid = properties.get("gridProperties") or {}
            row_count = max(int(grid.get("rowCount", 1)), len(rows), 1)
            column_count = max(
                int(grid.get("columnCount", 1)),
                required_columns,
                1,
            )
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "hidden": sheet.title == "_HealthAgent",
                            "gridProperties": {
                                "frozenRowCount": 1 if sheet.headers else 0,
                                "rowCount": row_count,
                                "columnCount": column_count,
                            },
                        },
                        "fields": (
                            "hidden,gridProperties.frozenRowCount,"
                            "gridProperties.rowCount,gridProperties.columnCount"
                        ),
                    }
                }
            )
            requests.append(
                {
                    "updateCells": {
                        "range": {"sheetId": sheet_id},
                        "rows": [_row_data(row) for row in rows],
                        "fields": "userEnteredValue",
                    }
                }
            )
            if sheet.headers:
                requests.append(
                    {
                        "repeatCell": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 0,
                                "endRowIndex": 1,
                            },
                            "cell": {
                                "userEnteredFormat": {"textFormat": {"bold": True}}
                            },
                            "fields": "userEnteredFormat.textFormat.bold",
                        }
                    }
                )
            for column in sheet.editable_columns:
                requests.append(
                    {
                        "setDataValidation": {
                            "range": {
                                "sheetId": sheet_id,
                                "startRowIndex": 1,
                                "startColumnIndex": column,
                                "endColumnIndex": column + 1,
                            },
                            "rule": {
                                "condition": {
                                    "type": "ONE_OF_LIST",
                                    "values": [
                                        {"userEnteredValue": value}
                                        for value in ("approve", "correct", "reject")
                                    ],
                                },
                                "strict": True,
                                "showCustomUi": True,
                            },
                        }
                    }
                )
        _execute(
            self._sheets.spreadsheets().batchUpdate(
                spreadsheetId=spreadsheet_id, body={"requests": requests}
            )
        )
