# Weekly coaching cycle implementation plan

**Goal:** Deliver the accepted shared weekly loop and correct retrospective food
identity/time, including privately repairing confirmed live records.

**Architecture:** FoodCoach delegates explicit labels/time/corrections to bounded
helpers. WeeklyCycle owns durable shared proposals, acceptance, evidence and notices;
existing domain stores and Telegram runtime provide I/O and idempotent delivery.
**Tech Stack:** Python, PilotStore/PostgreSQL, existing Telegram reply keyboards.

## Constraints

- Personal records/snapshots stay in ignored private data; tests use synthetic input.
- No calorie limit, prescribed intensity or silent acceptance of a proposed plan.
- Explicit meal evidence outranks reminders; raw messages and measured data survive.
- Main bot owns shared weekly pushes only when enabled; ordinary reminders remain.
- Existing user authorization covers this implementation and rollout.

## Task 1: Meal regressions and fix

- [x] Add tests in `tests/pilot/test_food_retrospective.py`: natural delayed reports,
  an explicit label overriding a reminder, pending clarification retaining original
  text/time, immediate label corrections, photo time/reminder anchoring and replay.
- [x] Update `food.py`/`food_conversation.py` with label-first routing, bound metadata
  corrections, capture/occurrence separation and unambiguous time extraction.
- [x] Run food tests and static checks. Prepare historical repair from source inputs
  in a cloned store; preserve audit and independently verify daily projection.

## Task 2: Shared weekly state

- [x] Add `pilot/weekly_cycle.py` for enabled settings, dated proposal/acceptance,
  replies, read-only bot context and due notices. Extract evidence/report helpers to
  `pilot/weekly_evidence.py`; data readers must be profile/period scoped.
- [x] Test proposal vs acceptance, stale button/date, replay/restart, goal periods,
  trip context, explicit focus/training mirroring and honest missingness.
- [x] Add bounded input actions for a self-reported workout, weight and weekly
  obstacle/result; retain provenance and avoid double counting COROS overlaps.
- [x] Generate Sunday review/next draft, Wednesday progress and initial invitation;
  quiet hours and bounded catch-up use stable notice keys.

## Task 3: Runtime integration

- [x] Route cycle commands through PilotActions and attach main-bot reply keyboards.
  Main dispatch sends shared notices; suppress separate legacy weekly notices only
  for profiles that opted into the shared cycle. Food/training contexts show the accepted shared plan.
- [x] Exercise runtime dispatch/replay, profile isolation, notification suppression
  and shared-plan routing. Run pilot/research/Telegram regression suites and Ruff/mypy.
- [x] Document commands, state/limits and operational verification in a runbook.

## Task 4: Rollout and concrete user result

- [x] Private snapshot and atomic historical repair; check unchanged meals and replay.
- [x] Persist owner cycle settings and the first explicit draft, without accepting it.
- [x] Fast-forward tested commits, restart conversations, verify successful polling
  and the first scheduled invitation/delivery receipt without manually sending chat.
- [x] Push GitHub, verify remote SHA, report fixed meal times and how to accept the
  concrete weekly proposal; clearly state any remaining unconfirmed food record.

Validation before rollout: 458 tests passed across pilot/research/Telegram; Ruff and
mypy passed. Historical repair and the initial draft were previewed in a private clone.

Rollout: the three conversation agents resumed successfully; their polls were fresh
and error-free. The main bot recorded delivery of the initial draft. No weekly plan
was accepted automatically. The source-backed meal repair preserved the existing
lunch and afternoon additions; source replay created no extra meals or model runs.
