# Confirmed Health Reminders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the confirmed health reminder scenario from durable proposal through Telegram confirmation, proactive delivery, snooze/reschedule, and completion/cancellation.

**Architecture:** PostgreSQL owns a constrained profile-scoped state machine and append-only events. A Telegram adapter executes deterministic commands, while a one-shot idempotent dispatcher runs every minute from its own local LaunchAgent and reuses `TelegramMessenger` delivery keys.

**Tech Stack:** Python 3.13, SQLAlchemy 2, PostgreSQL 18, Alembic, Typer, stdlib `zoneinfo`/`plistlib`, existing Telegram transport, pytest, Ruff, mypy.

## Global Constraints

- No reminder becomes active or deliverable without explicit confirmation.
- Every access is profile-scoped; Telegram derives the profile only from its bound private identity.
- PostgreSQL is authoritative; delivery is restart-safe and idempotent through stable `TelegramMessenger` keys.
- Every reminder stores source/provenance, reason, UTC due time, and an explicit validated IANA timezone; the CLI default is exactly `Europe/Moscow`.
- Scheduled operation uses local macOS launchd and no public server; no LLM executes lifecycle mutations.
- One profile or Telegram failure does not block other reminders or profiles.
- Implementation and tests must not mutate a live Telegram account, production database, or network service.

---

### Task 1: Durable reminder state machine

**Files:**
- Create: `src/health_agent/reminders/models.py`
- Create: `src/health_agent/reminders/repository.py`
- Create: `src/health_agent/reminders/time.py`
- Create: `alembic/versions/0006_health_reminders.py`
- Modify: `src/health_agent/models.py`
- Modify: `tests/conftest.py`
- Test: `tests/reminders/test_repository.py`
- Test: `tests/reminders/test_time.py`

**Interfaces:**
- Produces `ReminderRepository.propose`, `confirm`, `snooze`, `reschedule`, `complete`, `cancel`, `status`, `pending_proposals`, `due_occurrences`, `mark_*_delivered`.
- Produces `parse_local_datetime(value: str, timezone_name: str) -> datetime` returning aware UTC.

- [ ] Write failing tests proving the four-state lifecycle, idempotent compatible transitions, rejection of incompatible/cross-profile transitions, append-only events, timezone validation including DST gaps/folds, and migration constraints.
- [ ] Run `uv run pytest -q tests/reminders/test_repository.py tests/reminders/test_time.py` and verify the new imports/tables fail.
- [ ] Implement focused ORM models, the Alembic upgrade/downgrade, the transactional repository, immutable DTOs, and strict timezone conversion.
- [ ] Re-run the focused tests, `uv run ruff check src/health_agent/reminders tests/reminders alembic/versions/0006_health_reminders.py`, and `uv run mypy src`.
- [ ] Commit as `feat: add durable confirmed health reminders`.

### Task 2: Telegram command and delivery adapters

**Files:**
- Create: `src/health_agent/reminders/telegram.py`
- Create: `src/health_agent/reminders/dispatcher.py`
- Create: `src/health_agent/reminders/__init__.py`
- Modify: `src/health_agent/telegram/types.py`
- Modify: `src/health_agent/telegram/service.py`
- Modify: `src/health_agent/questions/composition.py`
- Test: `tests/reminders/test_telegram.py`
- Test: `tests/reminders/test_dispatcher.py`
- Modify: `tests/telegram/test_service.py`
- Modify: `tests/questions/test_composition.py`

**Interfaces:**
- Produces `DatabaseReminderCommands.handle(context: MessageContext, text: str) -> str | None`.
- Produces `ReminderDispatcher.run() -> DispatchReport` using stable proposal and occurrence delivery keys.

- [ ] Write failing tests for exact commands, bound-profile enforcement, safe malformed/stale responses, proposal formatting, due formatting, idempotent retry after send-before-ack, unknown/deferred delivery, and failure isolation across profiles.
- [ ] Run the focused reminder/Telegram tests and verify failures precede implementation.
- [ ] Implement deterministic parsers/formatters, repository-backed command handling without an LLM, dispatcher snapshot/ack flow, and optional reminder routing in the existing Telegram service.
- [ ] Compose real reminder commands in `build_telegram_question_runtime` without creating network activity during construction.
- [ ] Re-run focused tests, Ruff, and mypy; commit as `feat: connect reminders to Telegram`.

### Task 3: CLI and restart-safe macOS schedule

**Files:**
- Create: `src/health_agent/reminders/launchd.py`
- Modify: `src/health_agent/cli.py`
- Modify: `src/health_agent/config.py`
- Modify: `.env.example`
- Test: `tests/reminders/test_cli.py`
- Test: `tests/reminders/test_launchd.py`

**Interfaces:**
- Produces Typer commands `reminder propose`, `list`, `status`, `confirm`, `snooze`, `reschedule`, `complete`, `cancel`, `dispatch`, `render`, `install`, `automation-status`, `stop`, and `remove`.
- Produces a secret-free 60-second `com.orange.health-agent.reminders` user LaunchAgent.

- [ ] Write failing CLI tests with dependency substitution and plist tests that assert exact arguments, interval, local paths, private modes, idempotent lifecycle, and off-macOS errors without invoking `launchctl` or a network.
- [ ] Run those tests and verify they fail before production code exists.
- [ ] Implement thin CLI composition, safe content-free status/errors, a non-blocking dispatch lock, and a narrowly-owned LaunchAgent manager reusing existing private filesystem helpers.
- [ ] Run focused tests plus CLI `--help` smoke, Ruff, and mypy; commit as `feat: schedule confirmed reminders on macOS`.

### Task 4: Documentation and release gates

**Files:**
- Create: `docs/runbooks/reminders.md`
- Create: `docs/superpowers/reports/2026-09-05-confirmed-health-reminders-report.md`
- Modify: `README.md`
- Test: `tests/reminders/test_integration.py`

**Interfaces:**
- Proves the disposable-PostgreSQL scenario `proposal -> Telegram confirmation -> due delivery -> snooze -> redelivery -> complete`, including an unrelated second profile.

- [ ] Write the integration test using disposable PostgreSQL, fake Telegram gateway/state, and production repository/command/dispatcher composition; assert no pre-confirmation due delivery and no cross-profile access.
- [ ] Document setup, exact commands, timezone input, safe status, recovery, and that installation/live authorization are deliberately not performed by tests.
- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy .`, `uv lock --check`, `git diff --check`, and a disposable `uv run alembic upgrade head && uv run alembic check`.
- [ ] Self-review the complete branch for profile leaks, transition bypasses, unstable delivery keys, secrets in logs/plists, and missing restart cases; fix and repeat affected gates.
- [ ] Commit as `docs: document confirmed health reminders`.

