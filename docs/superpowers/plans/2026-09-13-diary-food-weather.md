# Diary, food and weather implementation plan

Goal: implement the user-requested fixes and contextual research collection, then verify live state.
Architecture: existing PilotStore for durable records; isolated parsing/classification/weather modules; reuse existing delivery receipts and quality scheduler. Python, SQLAlchemy/PostgreSQL, httpx, pytest.

- [x] Sleep: add `pilot/sleep_checkin.py`, integrate into `sleep.py`, default schedule/help 08:00. Tests in `tests/pilot/test_sleep_checkin.py` cover two tap-based subjective fields/nulls/duplicate replay/profile separation/restart and /опрос.
- [x] Food: update `food.py` routing to use delivered unanswered notices and explicit consumed-meal verbs, keep additions/questions separate, preserve idempotent retry; share snooze deadline semantics. Regression tests use the breakfast/lunch scenario with synthetic source keys and check two meals plus lunch-derived next target.
- [x] Food feedback: add `food_carbs.py` with ingredient-evidence categories; project into food history; replace weekly prose with one grounded action and a count. Tests distinguish whole grains, refined starch, sugar, fruit and unknown; coconut stays plant-based.
- [x] Weather: add `research/weather.py` with private config, HTTP client, raw/normalized storage, UTC filtering and per-day coverage/export. Add `research weather-sync` and once-daily integration in `participants.py`. Tests mock HTTP, reject malformed arrays, keep nulls, exclude future and verify idempotency.
- [x] Rollout: save private backup of corrected food rows; detach misclassified lunch, reanalyse own evidence; update owner sleep schedule; configure district coordinates, backfill Sept10 onward; restart main/food via existing lifecycle commands; check polls/QC/records. Run pilot/research tests, Ruff and focused mypy. Commit and push code/docs; verify remote commit. Record any source or collection limitations.

Implementation proceeds inline within the current authorized task; no agent delegation is needed.

Validation: 369 tests passed across pilot, research and Telegram. Focused mypy passed for 10 source files; Ruff passed. Live weather: 72 hourly rows, 24 per completed study day, all ten fields present, CSV hashes verified. All three bots poll without errors. The owner schedule is 08:00 Moscow. Food repair and backups stay private under data/research/repairs.
