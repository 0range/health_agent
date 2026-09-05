from __future__ import annotations

from sqlalchemy import inspect


def test_sheets_audit_tables_are_migrated(clean_database) -> None:
    tables = set(inspect(clean_database).get_table_names())
    assert {"sheets_sync_runs", "sheets_review_decision_audits"} <= tables
