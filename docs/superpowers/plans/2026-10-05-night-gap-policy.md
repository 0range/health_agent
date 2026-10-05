# Night gap policy implementation plan

**Goal:** Retain nights unless a required series has a continuous gap >600 seconds.

**Architecture:** A shared helper evaluates existing coverage diagnostics for
nightly research. Operational completeness warnings remain a separate decision.
The private study review imports the helper and retains its diagnostic fields.

**Tech stack:** Python, pytest, existing SQLAlchemy analysis, matplotlib.

## Constraints

- User-approved maximum gap: 600 seconds inclusive; coverage percentage is advisory.
- No interpolation, source edits, or health data in Git.
- Only observations from the start of the bedroom study inform the current report.

## Task 1 — Rule, documentation and boundary checks

- [x] Add `nightly_analysis_coverage_ok(check)` and
  `NIGHTLY_ANALYSIS_MAX_GAP_SECONDS = 600` to `src/health_agent/research/quality.py`.
  Return `check['samples'] > 0 and check['largest_gap_seconds'] <= 600`.
- [x] Add tests in `tests/research/test_analysis_coverage.py`: construct six-second
  observations over eight hours, remove an interior interval leaving gaps of
  66, 600 or 606 seconds; expected decisions True, True, False. Assert a 600-second
  gap remains accepted even below 99% coverage. Check leading/trailing gaps and
  an empty interval. Re-run the existing coverage test as an operational regression.
- [x] Document the dated rule in `docs/research/room-sleep-pilot.md` and distinguish
  it from collection warning thresholds in `docs/runbooks/research-collection.md`.

## Task 2 — Regenerate the private study review

- [x] Archive the existing ignored `data/research/reviews/2026-10-05-all-period/`
  outputs; import the helper in `analyze.py` and apply it to air, CO₂ and HR.
- [x] Record policy metadata, update methods, render eligibility and report text;
  use study-only summaries. Retain operational coverage diagnostics.
- [x] Re-run analysis, follow-up statistics, charts and report. Verify 18/24 eligible
  nights and inclusion of 14 September, unchanged exclusions for travel/current night,
  absence of obsolete 60-second analysis instructions, and inspect updated charts.
- [x] Commit and push only code/docs/tests after checks; keep the health artifacts private.

Validation: 9 focused tests passed, Ruff check/format and mypy passed. The real
14 September night is retained for both participants; study selection is 18/24
nights and only that night was added. Current private plots were visually checked.
