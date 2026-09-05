"""Connector contracts kept independent from SQLAlchemy and Google clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

type SheetValue = str | int | float | bool | None


@dataclass(frozen=True, slots=True)
class SheetsAccountIdentity:
    permission_id: str
    email: str


@dataclass(frozen=True, slots=True)
class WorkbookBinding:
    profile_id: str
    schema_version: str
    workbook_token: str


@dataclass(frozen=True, slots=True)
class CreatedWorkbook:
    spreadsheet_id: str
    spreadsheet_url: str


@dataclass(frozen=True, slots=True)
class ManagedSheet:
    title: str
    headers: tuple[str, ...]
    rows: tuple[tuple[SheetValue, ...], ...]
    editable_columns: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class WorkbookProjection:
    binding: WorkbookBinding
    sheets: tuple[ManagedSheet, ...]


class SheetsGateway(Protocol):
    def account_identity(self) -> SheetsAccountIdentity: ...

    def create_workbook(
        self, title: str, binding: WorkbookBinding
    ) -> CreatedWorkbook: ...

    def read_binding(self, spreadsheet_id: str) -> WorkbookBinding: ...

    def read_review_rows(
        self, spreadsheet_id: str
    ) -> tuple[tuple[SheetValue, ...], ...]: ...

    def replace_managed_tabs(
        self, spreadsheet_id: str, projection: WorkbookProjection
    ) -> None: ...
