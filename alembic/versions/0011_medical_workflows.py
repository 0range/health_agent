"""Merge reminders and doctor visits migration heads."""

revision = "0011_medical_workflows"
down_revision = ("0009_reminder_recurrence", "0010_doctor_visits")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
