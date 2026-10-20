# Pause Coach Bots Implementation Plan

**Goal:** Stop unused Telegram coaches and automatic morning sleep questions while
preserving the main conversation bot and independent source collection.

**Architecture:** Existing launchd disable/bootout operations and the existing
sleep schedule handler handle the user-facing shutdown. One optional CLI flag
exposes the existing daily COROS collection gate for an hourly one-shot job.

**Tech Stack:** Python, Typer, PostgreSQL, launchd.

## Constraints

- No deletion of observations, credentials or conversations.
- No Telegram test messages or fabricated diary entries.
- Explicit date-range COROS synchronization keeps its current behavior.
- No health data or credentials in tracked documentation.

## Task 1: Stop the requested interactions

- [x] Save private evidence of the two current plists and sleep schedule.
- [x] Persistently disable and boot out `com.orange.health-agent.pilot.food`
  and `com.orange.health-agent.pilot.training`.
- [x] Invoke `SleepCoach.handle(..., "/утро выкл", ...)` with a stable operation
  key; verify its saved schedule and that `due` emits no next-morning notice.

## Task 2: Preserve COROS collection independently

Files: `src/health_agent/pilot/cli.py`, `tests/pilot/test_coros_automatic_cli.py`,
`docs/runbooks/paused-coaches.md`.

- [x] Add `--automatic: bool = False` to `coros-sync`. In this branch call
  `maybe_coros_sync(store, profile_id, root, datetime.now(UTC))`, print whether
  an attempt occurred, then return. Reuse existing error handling and engine
  disposal; do not acquire a second lock around the helper.
- [x] Test that automatic sync delegates to the existing day/backoff gate without
  running a full-history sync, and that provider errors stay redacted.
- [x] Install an hourly, RunAtLoad, non-KeepAlive LaunchAgent invoking
  `health-agent pilot coros-sync --automatic`; use absolute paths and private logs.
- [x] Run focused tests and Ruff. Verify the loaded job's command and status.

## Task 3: Acceptance

- [x] Verify the main bot is polling and a synthetic request succeeds through
  its configured OpenAI adapter without Telegram delivery or fake records.
- [x] Confirm WHOOP and room jobs remain loaded. Record existing COROS/storage
  issues accurately; do not equate a configured token with working access.
- [x] Write private verification evidence, document reversal, commit and push
  the bounded code/documentation change.

Validation: 6 focused COROS CLI/runtime tests and 2 existing sleep-schedule tests passed; Ruff and diff checks passed. Live synthetic OpenAI request completed. Private operations evidence records persistent launchd disable flags and morning suppression. COROS refresh remains rejected by the provider; collection scheduling is retained independently.
