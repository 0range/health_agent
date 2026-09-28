# Three-field body check-in implementation plan

**Goal:** Collect dated weight, body fat and muscle measurements in the main bot.

**Architecture:** Deterministic parser, profile-scoped persistent questionnaire,
existing Telegram reply keyboard and existing shared weight history. Reuse the
current joint report for computed comparisons and LLM grounding.

**Tech stack:** Python 3.13, PilotStore/PostgreSQL, Telegram, pytest.

## Constraints

- Three optional values; preserve kg versus % for muscle mass.
- No fabricated measurements, dates, fat loss or screenshot support.
- The user selected Thursday/Sunday reminders at 08:00 Moscow; enable only for
  their profile, with a noon catch-up cutoff and stable date delivery keys.
- Date precision is day; actual recording time is a separate timestamp.
- Idempotent profile-scoped persistence and stale-button protection.
- Execute inline under the session's no-delegation rule.

## Task 1 — questionnaire and routing

- [x] Add `tests/pilot/test_body_checkin.py`: dated sequential and one-message
  entry, decimal commas, skipped values, kg/% distinction, invalid/future dates,
  unknown text passthrough, replay, other profile, stale buttons and partial save.
  Example behavioral assertions:
  ```python
  assert not store.list(profile, 'shared', 'weight')  # before Save
  assert saved.payload['muscle_percent'] == 42
  assert saved.payload['muscle_mass_kg'] is None
  assert saved.payload['measurement_date'] == '2026-09-27'
  ```
- [x] Run the new tests and observe the missing module failure.
- [x] Implement `src/health_agent/pilot/body_checkin.py` with
  `handle(store, profile, text, key, now) -> str | None` and `keyboard(text)`.
  Draft fields track answers independently of optional numerical values. Save
  uses `body:<draft source key>` for both observation and weight projection.
- [x] Route main text through the handler before WeeklyCycle in `runtime.py`;
  add questionnaire buttons to `SleepTelegramAPI` and update HELP.
- [x] Add `due(store, profile, now) -> list[Notice]`, settings toggles, and tests
  for Moscow weekdays, disabled settings, already-recorded dates and receipts.
- [x] Run the focused tests and existing runtime/sleep/cycle tests.

## Task 2 — grounded observations and delivery

- [x] Add joint-insight tests for exactly two days per week, composition units,
  future/other-profile isolation and day deduplication.
- [x] Extend `health_insights.py` daily/body_composition data and lower the weight
  comparison gate to two distinct days/week; retain explicit sample counts.
- [x] Update `health_insights_report.py` to describe preliminary changes, latest
  body measurements and the twice-weekly cadence without a prescribed calorie cap.
- [x] Document commands, date precision and limitations in a runbook, and update
  the existing joint-insights runbook.
- [x] Run affected suites, Ruff and mypy. Review diff for scope and privacy.
- [x] Fast-forward clean main, restart the main service after confirming no
  processing update, verify fresh polling and no error, push and verify HEAD.
- [x] If actual values have arrived, exercise production ingestion with their
  true date; otherwise leave the real-data test explicitly pending.

## Verification

802 tests passed across pilot, AI, Telegram, questions and insights. Ruff and
mypy passed on affected source files. A real user-provided measurement was
persisted with its reported date; the weight projection, retry idempotency and
main health-context inclusion were checked in production. All three bots resumed
fresh polling without errors. Thursday/Sunday reminders are enabled for the
owner. Private proof: `data/verification/body-checkin-2026-09-28/`. No manual
Telegram test messages were sent; scheduled delivery was checked without sending
a future reminder early.
