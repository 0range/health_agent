# WHOOP Hardening Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the remaining concurrency, crash-consistency, authorization-status, and long-rate-limit risks before live WHOOP authorization.

**Architecture:** Every per-account auth, sync, and status flow first acquires one orchestration `flock`; token-file and PostgreSQL locks are taken only inside it. Coordinated token replacement writes a durable journal keyed by a database `token_generation`, so recovery deterministically keeps a committed candidate or restores the previous token. Long rate-limit windows exit transactionally as a typed deferred result with `retry_at`.

**Tech Stack:** Python 3.13, httpx, SQLAlchemy, Alembic, PostgreSQL 18, Typer, pytest.

## Global Constraints

- Preserve profile/account isolation and all round-1 safety fixes.
- Never log or commit tokens, secrets, or real WHOOP payloads.
- Lock order is always account-operation lock before token-file or database locks.
- A reduced refreshed scope set is authorization failure, not a generic sync error.
- The amended pre-release `0005_whoop` has never shipped; retained review databases must be verified data-free and rebuilt.

---

### Task 1: Global account lock and durable token journal

**Files:**
- Modify: `src/health_agent/whoop/tokens.py`
- Modify: `src/health_agent/whoop/auth_service.py`
- Modify: `src/health_agent/whoop/models.py`
- Modify: `alembic/versions/0005_whoop.py`
- Test: `tests/whoop/test_tokens.py`
- Test: `tests/whoop/test_auth_service.py`

**Interfaces:**
- Produces: `TokenStore.operation(...)`, `TokenStore.recover(..., committed_generation)`, journal-backed `TokenReplacement`.

- [x] Write failing tests for auth/sync lock contention and interrupted prepared/published/committed journal states.
- [x] Add persistent per-account operation locks and document the global lock order.
- [x] Journal previous/candidate bundles before replace; make recovery select candidate only when its generation committed in PostgreSQL.
- [x] Mark the DB connection generation in the same transaction as token publication and clear the journal only after commit.
- [x] Eliminate post-replace chmod and inject fsync failure to prove recovery never silently exposes an uncommitted candidate.

### Task 2: Safe authorization and deferred sync outcomes

**Files:**
- Modify: `src/health_agent/whoop/client.py`
- Modify: `src/health_agent/whoop/sync.py`
- Modify: `src/health_agent/whoop/status.py`
- Modify: `src/health_agent/whoop/normalize.py`
- Modify: `src/health_agent/cli.py`
- Test: `tests/whoop/test_client.py`
- Test: `tests/whoop/test_sync.py`
- Test: `tests/whoop/test_cli.py`

**Interfaces:**
- Produces: `WhoopRateLimitDeferred(retry_at)`, safe `reauth_required`, `WhoopStatus.retry_at`.

- [x] Convert initial token read failures and missing required scopes into `WhoopAuthorizationRequired`.
- [x] Validate the complete required scope set before saving every refreshed token and when computing status readiness.
- [x] Convert every upstream numeric identity error to `WhoopNormalizationError` and persist a safe failed sync run.
- [x] Defer reset windows above the bounded inline wait, roll back partial data, and persist `retry_at` without holding a DB lock for hours.
- [x] Wrap auth, sync, and status in the account operation lock and prove concurrent auth/sync completes without deadlock.

### Task 3: Migration lineage, documentation, and gates

**Files:**
- Modify: `docs/runbooks/whoop.md`
- Modify: `docs/superpowers/plans/2026-09-04-health-agent-v1-whoop.md`
- Modify: `/Users/vitali.arz/Applications/VibeCoding/health-agent/.superpowers/sdd/2026-09-04-health-agent-v1-whoop/whoop-report.md`

**Interfaces:**
- Produces: a truthful clean-lineage handoff and final gate report.

- [x] Document that pre-fix `0005` was never deployed and give a fail-closed fingerprint/rebuild check for retained review databases.
- [x] Run empty upgrade, downgrade/upgrade, populated downgrade refusal, and metadata drift checks.
- [x] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src tests/whoop`, and `git diff --check`.
- [x] Commit without live credentials or push and append the exact results to the SDD report.
