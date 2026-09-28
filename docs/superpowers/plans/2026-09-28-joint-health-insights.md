# Joint Health Insights Implementation Plan

**Goal:** Deliver a grounded joint food/weight/WHOOP/COROS review in the main bot.

**Architecture:** A pure daily aggregation/analysis module, a profile-scoped WHOOP SQL
reader and a deterministic renderer feed existing weekly delivery and LLM context.

**Tech Stack:** Python, PostgreSQL, existing PilotStore, pytest.

## Constraints

Execute inline. No new external health-data transfers or fabricated measurement
dates. One existing Sunday delivery, stable replay keys, no change to the accepted
plan without user action. No personal records or credentials in git.

## Task 1: Dataset and computed findings

Files: `pilot/health_insights.py`, `pilot/health_insights_whoop.py`,
`tests/pilot/test_health_insights.py`.

- [x] Add tests for daily weight deduplication, sparse weight, food gaps and snacks.
- [x] Build `build_insights(store, profile, now, whoop)` with 28 complete daily rows.
- [x] Add `read_whoop(engine, profile, first, last, now)` for scored main sleep,
  linked recovery, sync status and a separately labelled undated body snapshot.
- [x] Implement week comparisons and the two gated association checks; test timezone
  alignment, away nights, nulls, score states, account/profile isolation and future data.

Contract: `assert report['weight']['delta_kg'] is None` when either week has fewer
than three recorded weight days. Run new tests with `PYTHONPATH=src` and main venv.

## Task 2: Main-bot report and shared context

Files: `pilot/health_insights_report.py`, `pilot/weekly_cycle.py`,
`pilot/runtime.py`, `pilot/sleep.py`, integration tests.

- [x] Render coverage, observed differences, limited associations and one next step.
- [x] Add idempotent `/инсайты` handling and include evidence in main LLM context.
- [x] Add the report to the existing Sunday message regardless of plan acceptance;
  leave push stop controls and delivery deduplication intact.
- [x] Keep manual `/вес` capture usable when the coaching cycle is paused.
- [x] Test actual action routing, replay, frozen weekly delivery and main context.

## Task 3: Verify and deploy

- [x] Preview on the user's real data read-only; save private evidence and show an
  honest current conclusion without pretending there is a fresh weight trend.
- [x] Run affected tests, Ruff and mypy; verify the scale's official integration path.
- [ ] Commit, fast-forward main, activate the owner setting with backup/audit,
  restart affected services and verify polling. Push and compare GitHub SHA.
- [ ] Document report semantics and clearly identify any weight-automation setup
  that still requires a phone/account action from the user.
