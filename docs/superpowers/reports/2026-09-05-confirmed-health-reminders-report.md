# Confirmed health reminders implementation report

Date: 5 September 2026

## Result

The v0.1 reminder vertical slice is implemented on branch
`codex/v1-confirmed-reminders` from merge base `b5147c7`. No live Telegram
request, production database mutation, network call, or LaunchAgent installation
was performed.

The delivered path is:

1. create a profile-scoped durable proposal with title, reason, source, due time,
   and explicit IANA timezone;
2. publish it idempotently to the bound private Telegram identity;
3. activate it only through an exact explicit confirmation command;
4. dispatch a confirmed due occurrence through stable `TelegramMessenger` keys;
5. snooze, reschedule, complete, or cancel through deterministic commands;
6. recover after process restart or a send-before-database-ack interruption
   without sending a duplicate.

## Safety and isolation

- PostgreSQL constraints reject a scheduled/completed row without confirmation.
- Every lookup and mutation uses both the bound `profile_id` and reminder code.
- Telegram text never supplies a profile ID.
- Action receipts are unique by Telegram bot/update key, so retrying a snooze
  after reply-spool failure cannot apply the duration twice.
- Pending proposals are never selected as due reminders.
- A row lock revalidates state and delivery revision immediately before send;
  deterministic proposal/occurrence keys provide the external idempotency fence.
- One profile's missing binding or delivery failure increments only a safe count
  and does not stop later reminders.
- Local wall times are rejected when an IANA-zone DST transition makes them
  ambiguous or nonexistent. A supplied numeric offset must match the zone.
- The LaunchAgent plist contains only paths, runs on a fixed 60-second interval,
  binds no server, and manages only `com.orange.health-agent.reminders`.

## Verification

- Focused reminder suite: `27 passed`.
- Full project pytest: `631 passed`.
- `uv run ruff check .`: passed.
- `uv run mypy src tests/reminders`: passed (`84 source files`).
- `uv lock --check`: passed.
- `git diff --check`: passed.
- Alembic fresh upgrade, downgrade/upgrade round trip, metadata check, and
  PostgreSQL constraints: passed inside the disposable PostgreSQL suite.
- CLI help and exact plist parse: passed.

Repository-wide `uv run mypy .` still reports 13 pre-existing test-typing errors
in automation, Google Drive, and staging tests. The same 13 errors reproduce at
the untouched merge base `b5147c7`; this branch adds none. Production `src` and
all new reminder tests are clean.

## Integration note

The branch includes the shared `TelegramTextActionService` seam from commit
`d6720da`. The parallel medical-review slice uses the same seam. When both are
integrated, production composition must keep one shared `PrivateReplyStore` and
wrap a single `CompositeTelegramTextActions` containing both the review and
reminder handlers. This branch already shares the spool between free-form
questions and reminder replies; it must not be replaced by a competing spool.

## Live steps deliberately deferred

After integration and owner credentials are ready: apply `alembic upgrade head`,
run one disposable/staging reminder, inspect the rendered plist, explicitly
install it, and confirm one real Telegram delivery. Those steps affect the
owner's live account and were outside this implementation run.

