"""Allow explicitly configured bounded initial backfill budgets."""

from alembic import op

revision = "0016_extraction_backfill_budget"
down_revision = "0015_pilot_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_extraction_daily_budget", "lab_extraction_profiles", type_="check"
    )
    op.create_check_constraint(
        "ck_extraction_daily_budget",
        "lab_extraction_profiles",
        "daily_budget BETWEEN 1 AND 500",
    )


def downgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            LOCK TABLE lab_extraction_profiles IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (SELECT 1 FROM lab_extraction_profiles WHERE daily_budget > 100) THEN
                RAISE EXCEPTION 'Refusing to downgrade configured extraction budget above 100';
            END IF;
        END $$;
    """)
    op.drop_constraint(
        "ck_extraction_daily_budget", "lab_extraction_profiles", type_="check"
    )
    op.create_check_constraint(
        "ck_extraction_daily_budget",
        "lab_extraction_profiles",
        "daily_budget BETWEEN 1 AND 100",
    )
