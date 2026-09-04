"""Add chart-ready normalization, medical dates, profiles, and source links.

Revision ID: 0004_chart_integrity
Revises: 0003_review_corrections
Create Date: 2026-09-04
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_chart_integrity"
down_revision: str | Sequence[str] | None = "0003_review_corrections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DEFAULT_PROFILE_ID = "00000000-0000-0000-0000-000000000001"


def upgrade() -> None:
    op.execute("DROP VIEW verified_lab_history")
    op.create_table(
        "profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute(
        "INSERT INTO profiles (id, name) "
        f"VALUES ('{_DEFAULT_PROFILE_ID}', 'Default')"
    )

    op.add_column("documents", sa.Column("profile_id", sa.Uuid(), nullable=True))
    op.add_column("documents", sa.Column("issued_date", sa.Date(), nullable=True))
    op.add_column("documents", sa.Column("collected_date", sa.Date(), nullable=True))
    op.add_column("source_records", sa.Column("profile_id", sa.Uuid(), nullable=True))
    op.add_column(
        "lab_observations", sa.Column("parsed_value", sa.Numeric(), nullable=True)
    )
    op.execute(
        f"UPDATE documents SET profile_id = '{_DEFAULT_PROFILE_ID}' "
        "WHERE profile_id IS NULL"
    )
    op.execute(
        f"UPDATE source_records SET profile_id = '{_DEFAULT_PROFILE_ID}' "
        "WHERE profile_id IS NULL"
    )
    op.execute(
        "UPDATE documents SET issued_date = (issued_at AT TIME ZONE 'UTC')::date "
        "WHERE issued_at IS NOT NULL"
    )
    op.execute(
        "UPDATE documents SET collected_date = (collected_at AT TIME ZONE 'UTC')::date "
        "WHERE collected_at IS NOT NULL"
    )
    op.alter_column("documents", "profile_id", nullable=False)
    op.alter_column("source_records", "profile_id", nullable=False)
    op.create_foreign_key(
        "fk_documents_profile", "documents", "profiles", ["profile_id"], ["id"]
    )
    op.create_foreign_key(
        "fk_source_records_profile",
        "source_records",
        "profiles",
        ["profile_id"],
        ["id"],
    )
    op.create_index("ix_documents_profile_id", "documents", ["profile_id"])
    op.create_index("ix_source_records_profile_id", "source_records", ["profile_id"])

    op.drop_constraint(
        "source_records_provider_external_id_revision_key",
        "source_records",
        type_="unique",
    )
    op.drop_constraint("documents_sha256_key", "documents", type_="unique")
    op.create_unique_constraint(
        "uq_source_records_profile_origin",
        "source_records",
        ["profile_id", "provider", "external_id", "revision"],
    )
    op.create_unique_constraint(
        "uq_source_records_id_profile", "source_records", ["id", "profile_id"]
    )
    op.create_unique_constraint(
        "uq_documents_profile_sha256", "documents", ["profile_id", "sha256"]
    )
    op.create_unique_constraint(
        "uq_documents_id_profile", "documents", ["id", "profile_id"]
    )

    op.create_table(
        "document_source_records",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_record_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
            name="fk_document_sources_document_profile",
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "profile_id"],
            ["source_records.id", "source_records.profile_id"],
            name="fk_document_sources_source_profile",
        ),
        sa.PrimaryKeyConstraint("document_id", "source_record_id"),
    )
    op.create_index(
        "ix_document_source_records_profile_id",
        "document_source_records",
        ["profile_id"],
    )
    op.execute(
        "INSERT INTO document_source_records "
        "(document_id, source_record_id, profile_id) "
        "SELECT id, source_record_id, profile_id FROM documents"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT 1 FROM documents d WHERE NOT EXISTS ("
        "SELECT 1 FROM document_source_records ds WHERE ds.document_id = d.id"
        ")) THEN RAISE EXCEPTION 'document source provenance backfill incomplete'; "
        "END IF; END $$"
    )
    op.drop_index("ix_documents_source_record_id", table_name="documents")
    op.drop_constraint(
        "documents_source_record_id_fkey", "documents", type_="foreignkey"
    )
    op.drop_column("documents", "source_record_id")
    op.drop_column("documents", "issued_at")
    op.drop_column("documents", "collected_at")

    op.execute(
        "UPDATE lab_observations SET "
        "parsed_value = replace(source_value, ',', '.')::numeric "
        "WHERE source_value ~ '^[+-]?([0-9]+([.,][0-9]+)?|[.,][0-9]+)$'"
    )
    _normalize_legacy_verified_rows()
    op.create_check_constraint(
        "ck_verified_observations_normalized",
        "lab_observations",
        "status <> 'verified' OR "
        "(parsed_value IS NOT NULL AND normalized_value IS NOT NULL "
        "AND normalized_unit IS NOT NULL)",
    )
    _create_verified_view()


def _normalize_legacy_verified_rows() -> None:
    unit_key = "lower(replace(trim(source_unit), 'μ', 'µ'))"
    supported = (
        "(canonical_name = 'ferritin' AND "
        f"{unit_key} IN ('ng/ml', 'нг/мл', 'ug/l', 'µg/l', 'мкг/л')) OR "
        "(canonical_name = 'vitamin_b12' AND "
        f"{unit_key} IN ('pg/ml', 'пг/мл')) OR "
        "(canonical_name = 'folate' AND "
        f"{unit_key} IN ('ng/ml', 'нг/мл')) OR "
        "(canonical_name IN ('total_cholesterol', 'ldl_cholesterol', "
        "'hdl_cholesterol', 'triglycerides') AND "
        f"{unit_key} IN ('mmol/l', 'ммоль/л')) OR "
        "(canonical_name = 'iron' AND "
        f"{unit_key} IN ('umol/l', 'µmol/l', 'мкмоль/л')) OR "
        "(canonical_name IN ('vitamin_d', 'prolactin') AND "
        f"{unit_key} IN ('ng/ml', 'нг/мл'))"
    )
    op.execute(
        "UPDATE lab_observations SET normalized_value = NULL, normalized_unit = NULL "
        "WHERE status = 'verified'"
    )
    op.execute(
        "UPDATE lab_observations SET "
        "normalized_value = parsed_value, "
        "normalized_unit = CASE "
        "WHEN canonical_name = 'ferritin' THEN 'ng/mL' "
        "WHEN canonical_name = 'vitamin_b12' THEN 'pg/mL' "
        "WHEN canonical_name = 'folate' THEN 'ng/mL' "
        "WHEN canonical_name IN ('total_cholesterol', 'ldl_cholesterol', "
        "'hdl_cholesterol', 'triglycerides') THEN 'mmol/L' "
        "WHEN canonical_name = 'iron' THEN 'µmol/L' "
        "WHEN canonical_name IN ('vitamin_d', 'prolactin') THEN 'ng/mL' END "
        "WHERE status = 'verified' "
        "AND source_value ~ '^[+-]?([0-9]+([.,][0-9]+)?|[.,][0-9]+)$' "
        f"AND ({supported})"
    )
    op.execute(
        "UPDATE review_items r SET decision = NULL, correction_json = NULL, "
        "resolved_at = NULL, reason_code = 'normalization_required' "
        "FROM lab_observations o WHERE r.observation_id = o.id "
        "AND o.status = 'verified' "
        "AND (o.parsed_value IS NULL OR o.normalized_value IS NULL "
        "OR o.normalized_unit IS NULL)"
    )
    op.execute(
        "INSERT INTO review_items "
        "(id, observation_id, reason_code, decision, correction_json, created_at, resolved_at) "
        "SELECT gen_random_uuid(), o.id, 'normalization_required', NULL, NULL, now(), NULL "
        "FROM lab_observations o LEFT JOIN review_items r ON r.observation_id = o.id "
        "WHERE o.status = 'verified' "
        "AND (o.parsed_value IS NULL OR o.normalized_value IS NULL "
        "OR o.normalized_unit IS NULL) "
        "AND r.id IS NULL"
    )
    op.execute(
        "UPDATE lab_observations SET status = 'needs_review' "
        "WHERE status = 'verified' "
        "AND (parsed_value IS NULL OR normalized_value IS NULL "
        "OR normalized_unit IS NULL)"
    )


def _create_verified_view() -> None:
    op.execute(
        "CREATE VIEW verified_lab_history AS "
        "SELECT observations.*, documents.profile_id, "
        "COALESCE(documents.collected_date, documents.issued_date) AS result_date "
        "FROM lab_observations observations "
        "JOIN documents ON documents.id = observations.document_id "
        "WHERE observations.status = 'verified'"
    )


def downgrade() -> None:
    op.execute("DROP VIEW verified_lab_history")
    op.drop_constraint(
        "ck_verified_observations_normalized", "lab_observations", type_="check"
    )
    op.execute("ALTER TABLE lab_observations DROP COLUMN IF EXISTS parsed_value")
    op.execute(
        "DO $$ BEGIN IF EXISTS ("
        "SELECT document_id FROM document_source_records "
        "GROUP BY document_id HAVING count(*) > 1"
        ") THEN RAISE EXCEPTION "
        "'cannot downgrade: documents have multiple source occurrences'; "
        "END IF; END $$"
    )
    op.add_column(
        "documents", sa.Column("source_record_id", sa.Uuid(), nullable=True)
    )
    op.execute(
        "UPDATE documents d SET source_record_id = ds.source_record_id "
        "FROM document_source_records ds WHERE ds.document_id = d.id"
    )
    op.alter_column("documents", "source_record_id", nullable=False)
    op.create_foreign_key(
        "documents_source_record_id_fkey",
        "documents",
        "source_records",
        ["source_record_id"],
        ["id"],
    )
    op.create_index(
        "ix_documents_source_record_id", "documents", ["source_record_id"]
    )
    op.drop_index(
        "ix_document_source_records_profile_id",
        table_name="document_source_records",
    )
    op.drop_table("document_source_records")

    op.add_column(
        "documents", sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "documents",
        sa.Column("collected_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.execute(
        "UPDATE documents SET issued_at = issued_date::timestamp AT TIME ZONE 'UTC' "
        "WHERE issued_date IS NOT NULL"
    )
    op.execute(
        "UPDATE documents SET collected_at = collected_date::timestamp AT TIME ZONE 'UTC' "
        "WHERE collected_date IS NOT NULL"
    )
    op.drop_column("documents", "issued_date")
    op.drop_column("documents", "collected_date")

    op.drop_constraint("uq_documents_id_profile", "documents", type_="unique")
    op.drop_constraint("uq_documents_profile_sha256", "documents", type_="unique")
    op.drop_constraint(
        "uq_source_records_id_profile", "source_records", type_="unique"
    )
    op.drop_constraint(
        "uq_source_records_profile_origin", "source_records", type_="unique"
    )
    op.create_unique_constraint(
        "documents_sha256_key", "documents", ["sha256"]
    )
    op.create_unique_constraint(
        "source_records_provider_external_id_revision_key",
        "source_records",
        ["provider", "external_id", "revision"],
    )
    op.drop_index("ix_documents_profile_id", table_name="documents")
    op.drop_index("ix_source_records_profile_id", table_name="source_records")
    op.drop_constraint("fk_documents_profile", "documents", type_="foreignkey")
    op.drop_constraint(
        "fk_source_records_profile", "source_records", type_="foreignkey"
    )
    op.drop_column("documents", "profile_id")
    op.drop_column("source_records", "profile_id")
    op.drop_table("profiles")
    op.execute(
        "CREATE VIEW verified_lab_history AS "
        "SELECT * FROM lab_observations WHERE status = 'verified'"
    )
