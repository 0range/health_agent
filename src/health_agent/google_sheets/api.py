"""Small mockable adapter for the official Google Sheets and Drive APIs."""

from __future__ import annotations

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

from health_agent.google_sheets.types import (
    CreatedWorkbook,
    ManagedSheet,
    SheetsAccountIdentity,
    SheetValue,
    WorkbookBinding,
    WorkbookProjection,
)

_MANAGED_TITLES = ("Lab history", "Needs review", "Sources", "_HealthAgent")


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, (TimeoutError, ConnectionError, socket.timeout)):
        return True
    if error.__class__.__module__.startswith("httplib2"):
        return True
    if not isinstance(error, HttpError):
        return False
    status = int(getattr(error.resp, "status", 0))
    return status == 429 or status >= 500


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
        payload = _execute(
            self._sheets.spreadsheets().create(
                body={"properties": {"title": title}, "sheets": sheets},
                fields="spreadsheetId,spreadsheetUrl",
            )
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
            .get(spreadsheetId=spreadsheet_id, range="'_HealthAgent'!A1:B3")
        )
        rows = payload.get("values")
        if not isinstance(rows, list) or len(rows) != 3:
            raise ValueError("invalid workbook binding")
        values: dict[str, str] = {}
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError("invalid workbook binding")
            key, value = row
            if not isinstance(key, str) or not isinstance(value, str) or key in values:
                raise ValueError("invalid workbook binding")
            values[key] = value
        if set(values) != {"profile_id", "schema_version", "workbook_token"}:
            raise ValueError("invalid workbook binding")
        return WorkbookBinding(
            values["profile_id"], values["schema_version"], values["workbook_token"]
        )

    def read_review_rows(
        self, spreadsheet_id: str
    ) -> tuple[tuple[SheetValue, ...], ...]:
        payload = _execute(
            self._sheets.spreadsheets()
            .values()
            .get(spreadsheetId=spreadsheet_id, range="'Needs review'!A:Z")
        )
        rows = payload.get("values", [])
        if not isinstance(rows, list):
            raise TypeError("invalid review grid")
        return tuple(tuple(cast(SheetValue, value) for value in row) for row in rows)

    def replace_managed_tabs(
        self, spreadsheet_id: str, projection: WorkbookProjection
    ) -> None:
        metadata = _execute(
            self._sheets.spreadsheets().get(
                spreadsheetId=spreadsheet_id,
                fields="sheets(properties(sheetId,title,hidden))",
            )
        )
        sheet_ids = {
            str(sheet["properties"]["title"]): int(sheet["properties"]["sheetId"])
            for sheet in metadata.get("sheets", [])
        }
        if set(sheet_ids) != set(_MANAGED_TITLES):
            raise ValueError("managed workbook tabs changed")
        managed = (
            *projection.sheets,
            ManagedSheet("_HealthAgent", (), _binding_rows(projection.binding)),
        )
        requests: list[dict[str, Any]] = []
        for sheet in managed:
            rows = (() if not sheet.headers else (sheet.headers,)) + sheet.rows
            sheet_id = sheet_ids[sheet.title]
            requests.append(
                {
                    "updateCells": {
                        "range": {"sheetId": sheet_id},
                        "rows": [_row_data(row) for row in rows],
                        "fields": "userEnteredValue",
                    }
                }
            )
            requests.append(
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet_id,
                            "hidden": sheet.title == "_HealthAgent",
                            "gridProperties": {
                                "frozenRowCount": 1 if sheet.headers else 0
                            },
                        },
                        "fields": "hidden,gridProperties.frozenRowCount",
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
