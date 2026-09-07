# Photo-first Food Journey Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. User approved parallel work; use disjoint owned files below. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A photo/comment-first food bot with automatic weekly reflection, food facts in the main Health Agent, and automatic Sunday training draft.

**Architecture:** Extend existing FoodCoach, Store and notice delivery. A bounded food-history projection is shared by the weekly reflection and main Brain. Reuse Telegram/Yandex/PostgreSQL; no new service or schema.

**Tech Stack:** Existing Python, SQLAlchemy, pytest, Telegram transport and Yandex Brain.

## Global Constraints

- User approved docs/superpowers/specs/2026-09-07-food-photo-first-design.md in chat, including written-spec review.
- Photos and ordinary comments are the normal interface; commands remain optional compatibility paths.
- A follow-up comment must not create another meal or reset its occurrence time/reminder. Save original before interpretation, link corrections by durable source key, replay must not modify a newer meal.
- User photo grouping extension: gap <40minutes from latest confirmed photo of current meal means same meal; gap >150minutes means new meal; inclusive40..150minutes requires one clarification. Compare actual Telegram event timestamps. Preserve every photo and ambiguous pending input. Reminder anchor is latest confirmed photo as estimated end, not a later text comment. Do not impose a one-hour maximum meal length.
- Only an explicit new-meal report or photo creates a meal; uncertain standalone text is preserved and clarified once. Questions do not become meals. No new automatic nutritionist restrictions.
- Weekly summary uses recorded facts only. Missing entries are not fasting; photo-derived nutrients are estimates, null is not zero. No causal or medical claims from counts.
- Separate profiles and domains. Main bot may receive bounded factual food records, never whole food chat or raw photo blobs. Training bot does not receive this projection.
- Preserve original photos, all inputs/comments, corrections and model output with no two-week deletion policy. Keep current timing, quiet-hours, pause/skip and notice replay guarantees.
- Do not touch private credentials, real user meals or earlier dirty docs/backlog.md/docs/research/. Root owns activation, final tests and docs. Existing deferred model-run audit error classification is outside this feature.

## Task 1: Photo comments and automatic weekly food reflection

**Files:** Modify src/health_agent/pilot/food.py, tests/pilot/test_food.py. Add tests/pilot/test_food_journey.py if focused journey tests are clearer. Do not edit runtime.py/brain.py/history helper (other owners).

**Interfaces:** FoodCoach constructor/handle/due unchanged. Consumes `build_food_history(store, profile_id, now, *, days=14)` from `health_agent.pilot.food_history` (Task2) returning the structure below. Notice uses the existing domain delivery acknowledgement.

- [ ] RED: actual photo meal → plain `Там ещё было немного масла` → `Порция примерно 250 г`: assert one meal, both comment originals retained, same occurred_at and reminder target, updated analysis receives the comments/photo. Replay a comment after another photo: assert it affects only the original meal and does not call Brain again after complete result.
- [ ] Keep existing command handlers first. Photo routes by the exact gap rule in Global Constraints; no existing meal means first meal. Preserve meal started/occurred_at, add latest photo/reminder anchor without overwriting initial time. Save every photo path in an append-only related record/list. For ambiguous photos, persist pending photo with source key and candidate meal ID before asking `Это продолжение того же приёма или новый приём?`; handle ordinary replies `тот же`/`новый` idempotently. Pending/late updates never silently modify a newer meal. Existing Brain image API accepts one image: analyse each photo individually and retain every per-photo result; meal aggregation may use those descriptions and comments, with unknown estimates explicit and repeated-view uncertainty. Never discard earlier meal components when a dessert arrives; do not blindly sum identical repeated views. Update due/snooze expiry to share the end anchor. Test39/40/150/151minute boundaries and photo replay.
- [ ] Text `/ел ...` and clear new-meal statements (`поел`, `съел`, `позавтракал`, `пообедал`, `поужинал`, explicit meal label with foods) route to a meal. For remaining text, use most recent same-Moscow-day meal within 4.5 hours as the comment candidate; clear general questions receive a short pointer to main Health Agent without creating food. A question about the current plate may be a comment/request to clarify its analysis. With no valid candidate, preserve the text and ask whether this is a new meal; never fabricate the meal.
- [ ] Add a durable `food/comment` record with source_key and `{meal_id,text}` before reanalysis. Use that stored meal_id on retry. Pass the saved comments and optional ordinary-text portion clarification into analysis, and retain previous derived output. Do not change occurrence time unless an explicit supported time-correction command is used. Original photo path stays unchanged. Model failure retains comment and reminder; incomplete analysis can be retried idempotently.
- [ ] Do not require a portion answer to accept a photo. Replace routine mandatory `/порция` prompt with honest approximate/unknown note; user may add detail naturally. Preserve existing numeric validation and framework-specific neutral plate feedback.
- [ ] Implement Sunday weekly notice at/after18:00 Europe/Moscow, only if there are actual records in previous7 local dates including today. Stable key `food-weekly-{ISOyear}-W{week}`. Cache reflection record before return; check delivered `notice` before work. Once generated fallback is stable, no model retry every poll. Existing meal reminders still run alongside weekly reflection even when latest meal is dinner; food reminder pause also suppresses unsolicited weekly notice. Manual `/неделя` returns the same kind of useful summary without requiring a schedule.
- [ ] Weekly prompt: concise Russian <=1200 chars, first one conclusion, then useful next step; assess recorded regularity, plate/framework, estimated nutrients and variety; mention insufficient recording and no unsupported precision. Include actual counted records/dates and within-day meal intervals (exclude overnight and post-dinner), known nutrient counts; do not assert total intake from incomplete logs. Structured output validated against supplied facts or render deterministic evidence framing with labelled model suggestions. On provider failure give short deterministic counts/coverage/interval fallback, no invented findings.
- [ ] GREEN: tests for questions not meals, missing candidate safe clarification, comment failure/retry, Sunday one delivery/restart, no-record silence, pause, dinner does not hide weekly reflection, nutrient unknowns, model outage stable fallback, other-profile isolation. Run `.venv/bin/pytest -q tests/pilot/test_food.py tests/pilot/test_food_journey.py` (omit absent new file), scoped Ruff/mypy. Commit only owned files and write report.

Core journey assertions:

```python
photo_reply = coach.handle(profile, '', source_key='photo1', now=noon, attachment=photo)
coach.handle(profile, 'Там ещё было немного масла', source_key='comment1', now=noon+timedelta(minutes=2))
assert len(store.list(profile, 'food', 'meal')) == 1
assert store.list(profile, 'food', 'comment')[0].payload['meal_id'] == first_meal.id
assert store.get(profile, first_meal.id).payload['occurred_at'] == first_meal.payload['occurred_at']
```

## Task 2: Reusable food-history projection and main-agent context

**Files:** Create src/health_agent/pilot/food_history.py, tests/pilot/test_food_history.py. Modify src/health_agent/pilot/brain.py and tests/pilot/test_brain.py. Do not edit FoodCoach/runtime.

**Interface:** `build_food_history(store: Store, profile_id: UUID, now: datetime, *, days: int = 14) -> dict[str, Any]`. Aware now; period includes today and days-1 preceding Moscow dates. No network or mutations. At most1000 source meals fetched, filter by payload occurred_at (corrected actual time), at most100 recent selected meals projected, disclose truncation. Include optional `ended_at` from latest confirmed photo when present; comments must not affect it. Return:

```python
{
    'period': {'days': days, 'from_date': start.isoformat(), 'to_date': today.isoformat()},
    'recorded_meal_count': len(selected),
    'recorded_days': sorted({local_day(m) for m in selected}),
    'truncated': truncated,
    'meals': [
        {'recorded_at': meal.payload['occurred_at'], 'category': meal.payload.get('category'),
         'foods': analysis.get('foods', []), 'portion_estimate': analysis.get('portion_estimate'),
         'nutrients_estimated': {key: analysis.get(key) for key in NUTRIENTS},
         'analysis_available': bool(analysis), 'unknowns': analysis.get('unknowns', [])}
    ],
    'interpretation_limits': 'Only logged meals; missing entries are not fasting. Nutrients are estimates, unknown is null.'
}
```

`NUTRIENTS` exact existing keys: kcal, protein_g, fat_g, carbs_g, saturated_fat_g, fiber_g, cholesterol_mg. Validate finite nonnegative numeric values again, reject bool, preserve null. No raw image paths, original full text, arbitrary model fields or conversation. Fixed fields only, bounded food/unknown strings. Unsupported/malformed dates are omitted with `invalid_record_count` disclosed, not silently asserted current.

- [ ] RED/GREEN tests: dates at Moscow boundary, corrected occurred_at not insertion order, null/zero differentiation, empty history, bounded/truncated history, invalid timestamp, two profiles, no mutation or private image/text leakage. Use MemoryStore, no production data.
- [ ] Implement projection with existing Store.list kind=meal, limit1000; validate days1..31. Owner1 imports this helper for weekly reflection; send root/owner1 message as soon as interface file exists.
- [ ] In PilotBrain._profile_context, add `recorded_food_history` using14days only when domain==sleep. Shared goals/Apple/source priorities remain intact. Extend rules: use facts as logs, not complete intake; never infer cause of weight/sleep changes from simultaneous logs alone; disclose absent data. No photo transfer or other-domain conversation copy.
- [ ] Tests in test_brain.py verify main context includes own food facts, excludes other profile/photo/text, and training context does not include food projection. Existing text/photo request behavior remains compatible.
- [ ] Run `.venv/bin/pytest -q tests/pilot/test_food_history.py tests/pilot/test_brain.py`; scoped Ruff/mypy. Commit only owned files, write report.

## Task 3: Automatic Sunday training orientation

**Files:** Modify src/health_agent/pilot/training.py and tests/pilot/test_training.py only. No food/Brain/runtime edits.

**Interface:** TrainingCoach unchanged. Read profile-scoped `training/settings`, source_key `weekly-preferences`: `{sessions: [{discipline: str, count: int}], strength_beginner: bool, source: 'user'}` seeded privately by root. Existing shared goals remain primary long-term context. No user-specific schedule hardcoded in code/tests.

- [ ] Add preferences to `_propose` payload along with current goals, actual activities, dialogue. Ask the model for an orienting next-week draft, adjustable to recovery/availability; beginner strength stays conservative without invented working weights or required intensity. Include explicit next-Monday through next-Sunday dates in payload, not inferred from model clock. Persist preferences snapshot and date range in the proposal.
- [ ] Change Sunday18:00 Moscow `due`: when explicit weekly preferences, goals, recent activities or accepted plan exist, prepare one combined concise notice containing current truthful reflection (when any actual data exists) and next-week proposal. A missing accepted plan is not permission to label sessions missed. Preparing a draft does NOT create accepted_plan; user need not accept it just to receive orientation.
- [ ] Stable weekly source keys: proposal `training-draft-{ISOyear}-W{week}`, notice retains existing training-weekly key. Cache generated draft before delivery, reuse on restart/provider outage. Honor already sent old weekly notice to avoid duplicate during migration. Keep <=1800 chars total, clear `Ориентир на следующую неделю, не обязательство` label. If provider fails, render saved desired discipline counts as desired slots, not completed activity, with no invented exercise prescription.
- [ ] Tests: Sunday with preferences and no activities still gives draft; actual records+draft combine in one notice; Monday silence; repeated due no extra Brain calls; delivered notice no resend; no implicit acceptance; own profile preferences only; real SDK failure fallback; next-week dates Monday boundary. Existing `/план` and `/сохранить план` still work.
- [ ] Run `.venv/bin/pytest -q tests/pilot/test_training.py`; scoped Ruff/mypy. Commit only owned files, report.

## Root integration and launch

- [ ] Review both task diffs against reports and approved spec; follow SDD fix/re-review caps.
- [ ] Update runtime help to photo/comment-first wording after domain review. Test existing HELP assertions without introducing new command requirements.
- [ ] Fake Telegram photo → PostgreSQL → comment → unchanged meal/reminder → weekly notice → once-only delivery; confirm no second meal. Run full suite once, Ruff/mypy.
- [ ] Final scoped feature review, then restart existing three labels; check recent poll/errors and send one concise readiness update. No real synthetic meals in production.
- [ ] Update docs/three-jobs-pilot.md and GitHub; preserve user-owned unrelated dirty files.
