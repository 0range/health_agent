"""Add bounded recurrence chains to reminders."""

import sqlalchemy as sa

from alembic import op

revision = "0009_reminder_recurrence"
down_revision = "0008_lab_extraction"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "health_reminders", sa.Column("repeat_unit", sa.String(10), nullable=True)
    )
    op.add_column(
        "health_reminders", sa.Column("repeat_every", sa.Integer(), nullable=True)
    )
    op.add_column(
        "health_reminders", sa.Column("recurrence_parent_id", sa.Uuid(), nullable=True)
    )
    op.create_check_constraint(
        "ck_health_reminders_recurrence",
        "health_reminders",
        "(repeat_unit IS NULL AND repeat_every IS NULL) OR "
        "(repeat_unit = 'days' AND repeat_every BETWEEN 1 AND 3650) OR "
        "(repeat_unit = 'months' AND repeat_every BETWEEN 1 AND 120)",
    )
    op.create_check_constraint(
        "ck_health_reminders_recurrence_not_self",
        "health_reminders",
        "recurrence_parent_id IS NULL OR recurrence_parent_id <> id",
    )
    op.create_unique_constraint(
        "uq_health_reminders_recurrence_parent",
        "health_reminders",
        ["recurrence_parent_id"],
    )
    op.create_foreign_key(
        "fk_health_reminders_recurrence_parent_profile",
        "health_reminders",
        "health_reminders",
        ["recurrence_parent_id", "profile_id"],
        ["id", "profile_id"],
    )


def downgrade() -> None:
    op.execute("""DO $$ BEGIN
        IF EXISTS (
            SELECT 1 FROM health_reminders
            WHERE repeat_unit IS NOT NULL OR repeat_every IS NOT NULL
               OR recurrence_parent_id IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'Refusing to downgrade reminders with recurrence state';
        END IF;
    END $$""")
    op.drop_constraint(
        "fk_health_reminders_recurrence_parent_profile",
        "health_reminders",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_health_reminders_recurrence_parent", "health_reminders", type_="unique"
    )
    op.drop_constraint(
        "ck_health_reminders_recurrence_not_self", "health_reminders", type_="check"
    )
    op.drop_constraint(
        "ck_health_reminders_recurrence", "health_reminders", type_="check"
    )
    op.drop_column("health_reminders", "recurrence_parent_id")
    op.drop_column("health_reminders", "repeat_every")
    op.drop_column("health_reminders", "repeat_unit")
