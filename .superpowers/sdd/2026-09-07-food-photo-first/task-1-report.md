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

## Fix round 1 — six Important review findings

Base: `c4a5237`; reviewed implementation: `643d80e`.

RED command:

```text
.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py
```

New reviewer-counterexample tests initially produced seven failures: distinct photo outputs were not aggregated immutably; late delivery moved a newer meal backwards; ordinary food words fabricated intake; next-day durable retry was unavailable; and weekly quiet-hours/shared evidence behavior was absent. Existing explicit test inputs that had relied on bare food nouns were changed to the supported optional `/ел` compatibility route.

Fixes by finding:

1. Each photo now retains its first parsed observation and raw output unchanged. Every successful derived result is appended to `analysis_revisions`. Meal aggregation conservatively unions distinct foods/components across photo observations and later clarification, nulls quantities instead of summing multi-view estimates, and records repeated-view uncertainty. The selected latest photo caption is supplied to analysis.
2. Photo grouping selects the latest confirmed photo at or before the Telegram event time, never an arrival-latest future meal. Confirming an older pending photo retains the maximum confirmed anchor, so `latest_photo_at`/`ended_at` cannot move backwards.
3. Only `/ел`, explicit completed-intake verbs, or a food-bearing meal label creates a text meal. Ingredient-like text and current-plate questions attach as comments when eligible; prospective/uncertain text is preserved for clarification.
4. Durable comment and confirmation source keys are resolved before current-day/candidate routing. Replays retry only their stored meal when incomplete, including next-day comment and failed photo/text confirmation retries.
5. Automatic weekly reflections now honor configured/default quiet hours before generating or returning cached unsolicited notices.
6. Manual `/неделя` and automatic Sunday delivery share the same reflection path. Evidence includes exact recorded dates, unique recorded-food variety, configured plate-framework coverage, per-nutrient known-estimate counts, and end-to-next-start intervals. Interval calculation excludes overnight intervals and every remaining adjacency after dinner.

Distinct-output tests use rice/vegetables, cake, and oil responses rather than the same fake response for every photo.

Final verification:

```text
.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py tests/pilot/test_photo_first_acceptance.py
42 passed (five unrelated SWIG deprecation warnings)

.venv/bin/ruff check src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
All checks passed!

.venv/bin/mypy src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
Success: no issues found in 3 source files

git diff --check
clean
```

Self-review: replay-first dispatch was checked for comments, photo confirmations, and text confirmations; event-time grouping was checked against the review's 12:00/15:00/12:10 sequence; immutable observations were checked after comment reanalysis; cached weekly output remains stable after provider failure and is suppressed during quiet hours. `/время` remains the documented current start-time correction and was intentionally not expanded in this round. No known remaining concern in the six requested findings.

## Fix round 2 — three remaining Important findings

Base: `5f43917`.

RED command:

```text
.venv/bin/pytest -q tests/pilot/test_food_journey.py -k 'explicit_text_meal_is_a_boundary or question_containing_intake or weekly_long_food'
```

Result before fixes: 3 failed. The photo after an intervening explicit text meal remained on the older photo meal; a question containing `поел` created a meal and called the model; an outage fallback with eight valid long food names was 1,979 characters.

GREEN changes:

- Event-time meal selection now considers all actual meals, including explicit text meals. A first photo enriches the current text-only meal and establishes its immutable original photo path instead of reviving an older photo meal or creating a third meal.
- Clear questions are classified before ordinary intake verbs. Current-plate questions still attach to an eligible current meal, while general questions remain routed to main Health Agent.
- Weekly food examples receive an explicit shared character budget. The final renderer independently enforces 1,200 characters while reserving the complete incomplete-log caveat and useful next-step suffix. Model suggestions are appended only when the whole result fits; no blind final slicing is used.

Final verification:

```text
.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py tests/pilot/test_photo_first_acceptance.py
45 passed (five unrelated SWIG deprecation warnings)

.venv/bin/ruff check src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
All checks passed!

.venv/bin/mypy src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
Success: no issues found in 3 source files

git diff --check
clean
```

Self-review checked the exact 12:00 photo → 12:10 `поел суп` → 12:20 photo sequence, a no-context `Почему я поел и хочу спать?` question (zero meals and zero model calls), and both manual/automatic provider-failure reflections with projected 200-character food values. The previously accepted replay, quiet-hours, and aggregation areas were not reopened. No known concerns remain in this round.

## Fix round 3 — stale text-only meal eligibility

Base: `08b5b60`.

RED command:

```text
.venv/bin/pytest -q tests/pilot/test_food_journey.py -k 'explicit_text_meal_is_a_boundary or stale_text_only'
```

Result before fix: 1 passed, 1 failed. The nearby 10-minute text-meal boundary worked, but a September 14 photo attached to a September 7 text-only meal, leaving one meal and moving the old meal's anchor.

GREEN: a current text-only meal now uses its real `occurred_at` as a provisional grouping anchor. The existing exact rule applies unchanged: under 40 minutes attaches, inclusive 40–150 minutes persists an ambiguity, and over 150 minutes starts a new photo meal. Once confirmed, the first photo establishes `latest_photo_at` and the normal last-photo anchor behavior takes over. No photo timestamp is synthesized.

Final verification:

```text
.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py tests/pilot/test_photo_first_acceptance.py
46 passed (five unrelated SWIG deprecation warnings)

.venv/bin/ruff check src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
All checks passed!

.venv/bin/mypy src/health_agent/pilot/food.py tests/pilot/test_food.py tests/pilot/test_food_journey.py
Success: no issues found in 3 source files

git diff --check
clean
```

Self-review verified both the accepted nearby sequence and the exact September 7 → September 14 counterexample. The stale photo creates a new meal at the event time and leaves the old meal payload byte-for-byte unchanged. No other reviewed behavior was modified and no known concern remains.
