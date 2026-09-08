# Food feedback completion Implementation Plan

> Execute inline in this session; this finishes the already approved photo-first and sleep-diary scenarios.

**Goal:** Show approximate calories and the next meal time after a food photo, classify plant milk correctly, and verify the installed v01 archive and sleep capture.

**Architecture:** Keep FoodCoach and its durable records. Render feedback locally from validated analysis. Share the meal interval calculation between reply and reminders. Preserve original photos, model output and revisions.

**Tech Stack:** Python, SQLAlchemy/PostgreSQL, pytest, existing Yandex and Sheets/Metabase adapters.

## Constraints

- No synthetic records in the live profile; tests use MemoryStore or disposable PostgreSQL.
- Approximate nutrients must be labelled; unknowns remain unknown. Existing meal timing and quiet-hour controls remain authoritative.
- Preserve unrelated working-tree changes and original medical evidence.

### Task 1: Food regression and fixes

Files: `src/health_agent/pilot/food.py`, `tests/pilot/test_food_feedback.py`.

- [x] Reproduce coconut milk incorrectly classified as dairy; verify mixed real dairy remains present.
- [x] Verify numeric calories reach the reply, uncertain calories stay explicit, later correction replaces old ingredient labels.
- [x] Request a rough photo-based nutrient estimate with stated assumptions; validate numeric estimates. Keep multi-photo unknown totals rather than double-counting.
- [x] Render next meal from the same target as the reminder, including model failure, comments, grouped photos, dinner and quiet/pause controls.
- [x] Run focused food/pilot tests and Ruff/mypy.

### Task 2: Installed verification and reconciliation

Files: private `data/pilot/verification/` reports and current documentation.

- [x] Read the main bot diary and source turn, check runtime routing and heartbeat.
- [x] Reanalyse the actual breakfast once with the fixed prompt; preserve revisions and timestamps and inspect the result without sending Telegram messages.
- [x] Inventory remaining lab candidates/page states and previous source-audit completion reports; finish pending projection refresh and verify remote rows against the database.
- [x] Run required regression checks, restart affected services, verify fresh heartbeats, record results and limits.
