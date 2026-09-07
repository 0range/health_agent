# Task 2 report: bounded food-history projection

## Outcome

- Added the pure `build_food_history` projection with Moscow-date filtering, corrected
  `occurred_at` ordering, profile isolation, nutrient revalidation, fixed safe fields,
  optional photo-derived end estimates, invalid-record disclosure, and truncation.
- Capped the serialized projection at 20,000 characters by dropping oldest projected
  meals while retaining source records unchanged.
- Added sleep-domain main-agent context for recorded food facts and a separately
  labelled, fixed-whitelist user-selected food framework. Training context remains
  unchanged.
- Expanded system rules so logs are not treated as complete intake or causal evidence.

## Verification

- `.venv/bin/pytest -q tests/pilot/test_food_history.py tests/pilot/test_brain.py`
  - 13 passed
- `.venv/bin/ruff check src/health_agent/pilot/food_history.py src/health_agent/pilot/brain.py tests/pilot/test_food_history.py tests/pilot/test_brain.py`
  - All checks passed
- `.venv/bin/mypy src/health_agent/pilot/food_history.py src/health_agent/pilot/brain.py tests/pilot/test_food_history.py tests/pilot/test_brain.py`
  - Success: no issues found in 4 source files

## Self-review

- Projection is read-only and requests no more than 1,000 meal records.
- No original text, raw image path, model feedback, arbitrary analysis fields, or
  another profile's records enter the projected context.
- Zero remains distinct from unknown/null; booleans, negative and non-finite nutrient
  values are rejected.
- An `ended_at` estimate may be later than query time and remains explicitly labelled
  `last_photo_plus_20m`; comments are not consulted.
- No runtime, FoodCoach, provider, credential, real-data, or restart changes were made.
