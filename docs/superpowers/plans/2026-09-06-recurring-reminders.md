# Recurring Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** A confirmed repeating checkup creates exactly one next occurrence after completion and can be managed in Telegram.

**Architecture:** Extend the existing reminder repository and delivery lifecycle. Each occurrence is a normal reminder with immutable historical completion and a unique parent link; no separate scheduler.

**Tech Stack:** Python, PostgreSQL, Alembic, existing Telegram actions.

## Global Constraints

- Preserve one-shot behavior, profile isolation, exact reply/action idempotency, existing delivery fences and no automatic medical interval selection.
- No live DB, credentials, external calls or real Telegram messages by implementer/reviewer.
- Only owned reminder files, new migration and tests/docs. Root registers any new CLI; do not edit shared cli.py, config.py, tests/conftest.py or panel code.
- User explicitly requested parallel development and no intermediate user approval gates; isolated worktrees override the SDD default single-implementer sequencing.

### Task 1: Recurrence lifecycle and Telegram entrypoints

**Files:** modify `src/health_agent/reminders/models.py`, `repository.py`, `telegram.py`, `time.py`; create `alembic/versions/0009_reminder_recurrence.py`, `tests/reminders/test_recurrence.py`; extend owned reminder tests; add `docs/runbooks/recurring-reminders.md`.

**Contracts:** migration revision `0009_reminder_recurrence`, down `0008_lab_extraction`. Nullable `repeat_unit` (`days` or `months`), `repeat_every` (1..3650 days or 1..120 months), `recurrence_parent_id` UUID; unique parent and same-profile composite FK to health_reminders. Add nullable recurrence fields with defaults at end of immutable Reminder dataclass to preserve existing constructors. DB CHECK requires both unit/every set or both null, validates range. Parent cannot self-reference. Existing one-shot rows unchanged. Downgrade refuses to drop populated recurrence state.

Repository.propose accepts optional repeat_unit/repeat_every; invalid pair rejects before writes. Add `successor(profile_id, public_code)` returning next Reminder or None. `complete` locks parent, uses existing replay handling, completes it, and for repeating parent inserts+confirms one successor in the same transaction (series confirmation already supplied). Successor copies content, timezone and recurrence interval, but gets new id/code and no notification/delivery/completion state. Replayed completion (same or new action key) returns original completed parent and never creates another child. Cancel the active successor prevents further occurrences. Foreign-profile read/cancel/complete/replay is rejected.

Next due: start from the later of previous due_at and completion timestamp, convert to reminder timezone, add calendar days/months (month-end clamp), preserving that base's local wall clock. This means a late checkup starts the next interval from actual completion. For DST gap move forward to the first valid minute (bounded 180 minutes), ambiguous time uses earlier instant; invalid/overflow dates reject transaction safely. Do not create a backlog of missed reminders.

Telegram: extend existing handler with `/reminders` (bounded list of current pending/scheduled occurrences, first 20), `/reminder_new YYYY-MM-DDTHH:MM | title | optional Ndays or Nmonths`; default timezone Europe/Moscow, explicit source_type=user and source_reference=telegram. Creation produces a proposal with existing confirm/cancel commands; duplicate update must not create duplicate proposal (derive a stable public code from bot+update identity and reject altered replay). No NLP side effects. Existing `/reminder_done` reply should include successor due date/code for repeats and cancellation instructions. Show repeat interval in proposal/due messages. Provide concise help for bad format.

- [ ] **Step 1: RED.** Add pure time and DB regressions, recording failures before implementation. Example: propose+confirm a 12-month reminder due 2026-09-06, complete at its due time, assert one scheduled successor due 2027-09-06; complete again with a different action key, assert still one successor. Test 2028-02-29 month/year clamp, late completion, DST gap/fold, invalid ranges, foreign profile, one-shot unchanged.

```python
parent = repository.complete(profile_id, code, now=due, action_key='done-1')
child = repository.successor(profile_id, code)
assert parent.status is ReminderStatus.COMPLETED
assert child.status is ReminderStatus.SCHEDULED
repository.complete(profile_id, code, now=due, action_key='done-2')
assert repository.successor(profile_id, code).id == child.id
```

- [ ] **Step 2: Implement migration/repository/time contracts.** Reuse locks, confirmation, events and validation. Tests use existing disposable DB fixture only; self-referential nullable FK cleanup must still work with table-wide DELETE. Never modify production.
- [ ] **Step 3: Implement Telegram routes/messages and tests.** Test new/list/confirm/done/cancel chain, duplicate incoming update, invalid syntax and foreign profile. Exercise real repository with fake outbound transport; do not treat a canned string as end-to-end delivery.
- [ ] **Step 4: Verify and commit.** Focused reminders tests incl dispatch/replay, Ruff changed files, `mypy src`, `git diff --check`. Root owns combined full suite and integration. Report RED/GREEN, commands/results and concerns; commit only owned files.
