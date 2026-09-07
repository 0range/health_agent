# Task 1 report — photo/comment-first food journey

Status: DONE

## Scope

- Modified only `src/health_agent/pilot/food.py` and food-owned tests.
- Added `tests/pilot/test_food_journey.py`.
- Consumed the Task 2 `build_food_history(...)` interface without editing its files.
- Did not touch runtime, brain, credentials, production data, or runner processes.

## RED

Command:

```text
.venv/bin/pytest -q tests/pilot/test_food_journey.py
```

Result before implementation: 10 failed. Failures demonstrated that comments created extra meals, photos were neither grouped nor durably recorded, exact gap boundaries were absent, questions became meals, comment retry was unbound, weekly notices were absent, and reminders ignored the photo end anchor.

## GREEN

Implemented:

- Durable append-preserving photo records and meal photo lists; original `photo_path` remains the first photo.
- Exact photo grouping: `<40m` same, `>150m` new, inclusive `40..150m` persisted pending clarification.
- Idempotent ordinary `тот же` / `новый` confirmation bound to the pending photo's original candidate.
- `ended_at = latest confirmed photo + 20m` and `end_source = last_photo_plus_20m`; reminder target, expiry, and snooze expiry share that anchor.
- Durable comments bound by `meal_id` and `source_key`, saved before reanalysis, replay/retry bound to the original meal, natural portion clarification supported.
- Per-photo analysis retention, prior derived output retention, all comments supplied on reanalysis, and original-photo replay safety.
- Conservative text routing: explicit meal statements create meals; current-meal comments attach within the same Moscow day and 4.5 hours; general questions point to main Health Agent; uncertain standalone text is persisted and confirmed without fabrication.
- Removed the routine mandatory `/порция` demand while preserving the optional command and numeric/structured validation.
- Sunday at/after 18:00 Moscow weekly reflection using seven-day projected facts, stable ISO-week key, cached text/fallback before return, delivered-notice suppression, profile isolation, pause suppression, and coexistence with dinner/reminders.

Final test command:

```text
.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py tests/pilot/test_photo_first_acceptance.py
```

Result: 35 passed; five unrelated SWIG deprecation warnings.

## Static checks

```text
.venv/bin/ruff check src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
All checks passed!

.venv/bin/mypy src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
Success: no issues found in 3 source files

git diff --check
clean
```

## Self-review

- Verified exact 39/40/150/151-minute behavior and both ambiguous decisions.
- Verified confirmation, photo, comment, provider-failure, and restart replay paths do not mutate a newer meal.
- Verified comment timestamps never change `occurred_at` or the reminder anchor.
- Verified weekly generation checks delivered notice first and never retries a cached provider fallback on polling.
- Verified the root-owned real Telegram/Postgres acceptance test passes.
- No known correctness or scope concerns remain.
