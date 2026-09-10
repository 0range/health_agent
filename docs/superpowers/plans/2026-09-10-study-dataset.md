# Shared-room study dataset implementation plan

**Goal:** Private reproducible daily comparison datasets for two WHOOPs and one room sensor.

**Architecture:** `research/dataset.py` reads existing profile/account-scoped archives and builds native exports plus a six-second comparison table. `research export --day` runs manually; `check_participants` regenerates completed days after its existing checks. Execute inline.

**Tech stack:** Python standard CSV/JSON, SQLAlchemy, existing PostgreSQL and launchd.

## Constraints

14–21 nights; retain raw timestamps and revisions; no interpolation; no synthetic UTC stress times; no extra messages or publication; keep existing cadence and token handling.

- [x] Implement pure six-second aggregation with first/last timestamps and missing bins; write native observations and all source fields into private day directories using atomic writes.
- [x] Select latest detail versions by profile, remote user, calendar date and last_seen_at; export native stress graph variants and exact stage intervals; include public source_values for sleep/recovery/cycle/workout/body.
- [x] Add manual export CLI and automatic completed-day regeneration to existing quality job; expose export failures in aggregate status.
- [x] Test bin boundaries/duplicates/gaps, cross-profile filtering and unverified stress timestamps. Run relevant tests and static checks.
- [x] Generate and inspect today's partial export; confirm two-account collection and disk status. Update study duration and runbook, commit and push.

Validation: 82 related tests passed, including automatic previous-day export at 10:00 Moscow, profile isolation, empty intervals and file hashes. Ruff and mypy passed. Today's explicitly partial live export has 14,400 comparison intervals, 63 columns, two HR series, all eight room fields, native stress variants and sleep/recovery context. Both account renewals and scheduled collection were verified; current quality/storage status is ok.
