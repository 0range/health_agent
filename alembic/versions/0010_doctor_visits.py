"""Profile-scoped clinician visits and append-only questions/answers."""

import sqlalchemy as sa

from alembic import op

revision = "0010_doctor_visits"
down_revision = "0008_lab_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "health_visits",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "profile_id", sa.Uuid(), sa.ForeignKey("profiles.id"), nullable=False
        ),
        sa.Column("public_code", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone_name", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("creation_key", sa.String(200), nullable=False),
        sa.Column("creation_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("id", "profile_id", name="uq_health_visits_id_profile"),
        sa.UniqueConstraint("public_code", name="uq_health_visits_public_code"),
        sa.UniqueConstraint("creation_key", name="uq_health_visits_creation_key"),
        sa.ForeignKeyConstraint(
            ["source_document_id", "profile_id"],
            ["documents.id", "documents.profile_id"],
            name="fk_health_visits_document_profile",
        ),
        sa.CheckConstraint(
            "status IN ('planned','completed','cancelled')",
            name="ck_health_visits_status",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_health_visits_interval"),
    )
    op.create_table(
        "health_visit_notes",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("visit_id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("text", sa.String(10000), nullable=False),
        sa.Column("action_key", sa.String(200), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["visit_id", "profile_id"],
            ["health_visits.id", "health_visits.profile_id"],
            name="fk_health_visit_notes_visit_profile",
        ),
        sa.UniqueConstraint("action_key", name="uq_health_visit_notes_action_key"),
        sa.CheckConstraint(
            "kind IN ('question','answer')", name="ck_health_visit_notes_kind"
        ),
    )
    for table, columns in (
        ("health_visits", ("profile_id", "starts_at")),
        ("health_visit_notes", ("profile_id", "visit_id")),
    ):
        for column in columns:
            op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (SELECT 1 FROM health_visits) OR EXISTS (SELECT 1 FROM health_visit_notes)
        THEN RAISE EXCEPTION 'Refusing to downgrade visits with existing state';
        END IF;
    END $$""")
    op.drop_table("health_visit_notes")
    op.drop_table("health_visits")
