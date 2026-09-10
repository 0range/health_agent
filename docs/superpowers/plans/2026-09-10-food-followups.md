# Food followups implementation plan

> Execute inline within the owner's authorized bug fixes. Keep all real messages,
> readings, credentials and repair backups outside Git.

**Goal:** Reliable breakfast prompts, durable meal additions and verifiable ingestion evidence.

**Architecture:** FoodCoach retains its delivery/store contracts. A small pure helper
parses unambiguous addition statements and reconciles them with model analysis.
Existing minute/hourly collectors supply the verification data independently.

## Tasks

- [x] Add focused regressions for next-morning breakfast after dinner, once-per-day
  receipt/restart behavior, already eaten, quiet/pause, expiry and configurable time.
  Implement in `pilot/food.py`; document `/завтрак` in runtime help.
- [x] Test planned versus confirmed additions, model omission/failure, replay,
  negative/question text, preserving the main plate and portion/time anchors.
  Implement `pilot/food_additions.py`, connect evidence and feedback in FoodCoach.
  Preserve scalar `foods` and `unknowns` returned by the real model.
- [x] Back up affected live records to private files; reparse scalar outputs and
  reanalyse the actual lunch with the owner's current confirmed-bread statement.
  Verify composition and calorie assumptions, keeping all original evidence.
- [x] Export actual sensor/WHOOP rows to ignored CSV files with timestamps and units;
  compare source readings with stored samples and measure independent archive growth.
  Write a human-readable private report with reproducing SQL and explicit limits.
- [x] Run relevant pilot/reminder/runtime tests and lint/types; restart only the food
  service, check heartbeat and next breakfast target, commit/push code and public docs.


## Verification evidence

- 241 pilot tests passed; changed modules passed Ruff and mypy.
- Food service restarted successfully and resumed Telegram polling without errors.
- Private repair preserved all original meal payloads, repaired six scalar-food
  interpretations and reanalysed the actual lunch with confirmed bread. Apple
  remains planned; the meal/photo timestamps and next-meal target were preserved.
- Read-only next-morning preview returned exactly the expected breakfast notice
  for 11 September, with no actual test message or synthetic meal stored.
- Re-requested WHOOP HR matched all 8,321 archived points. A Qingping source
  window matched all 50 returned records; the database also contains one boundary
  point not returned in that API window, stated explicitly in the private report.
- Without this verification script inserting measurements, live air count grew
  from 994 to 1,096 during the check. Native times, source comparison results,
  CSV exports and reproducing queries are in private data/research/verification/.
