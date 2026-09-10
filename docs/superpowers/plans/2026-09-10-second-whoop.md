# Second WHOOP Implementation Plan

**Goal:** Collect and check two separately authorized WHOOP accounts against one bedroom sensor.

**Architecture:** Private target manifest, independent existing DetailService operations, and profile-scoped QualityService runs aggregated by the existing quality CLI. Execute inline in this session.

**Tech Stack:** Python, Typer, SQLAlchemy/PostgreSQL, launchd, pytest.

## Global constraints

No credentials or participant identifiers in Git; preserve first participant configuration and original sample times. Continue other participant collection after a per-target failure. Only enrolled profiles enter research checks.

## Tasks

- [x] Add `whoop_detail_accounts_file: Path | None` settings and `whoop/participants.py`. A frozen target holds `profile_id`, `session_file`, and `root`. `targets(settings)` returns the existing default target plus declared additional targets; reject duplicate resolved paths and profile UUIDs.
- [x] Update `whoop/details_cli.py` to run each target under its own existing lock, validate declared profile against session, continue after exceptions, and return failure if any target failed. Keep single-target behavior and launchd label compatible.
- [x] Add optional `profile_id` to `QualityService`; use it for WHOOP connection and HR lookups, while air queries continue using sensor.profile_id. Update quality CLI to run enrolled profiles, persist their own status and daily reports, and aggregate failures and source states into the main report.
- [x] Add synthetic tests for duplicate targets, cross-profile session rejection, one failed collector not blocking another, and stale second-person HR not masked by fresh first-person samples. Run research and related CLI tests plus Ruff/mypy on changed modules.
- [ ] Verify both authorizations match the second profile, full-sync public resources, configure private manifest, sync detailed resources, and run research check. Confirm automatic refresh, launchd configuration and real per-person row counts. Update runbook, commit and push reviewed changes.

Validation: 79 related tests passed; Ruff and mypy passed. Live app-session identity and renewal verified. Both HR/stress diagnostic archives verified; second public OAuth is still pending owner completion. The active quality report correctly returns attention for this incomplete connection.
