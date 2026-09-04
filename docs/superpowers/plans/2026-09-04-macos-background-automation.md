# macOS Background Automation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one safe local runner and one four-hour macOS LaunchAgent for configured WHOOP, Gmail, and Drive synchronization.

**Architecture:** An extensible registry discovers profile-owned connector targets and executes existing connector CLI commands behind a global nonblocking lock. A private atomic state file schedules per-job weekly full reconciliation; a separate launchd service renders and manages one secret-free user plist.

**Tech Stack:** Python 3.13, Typer, SQLAlchemy, plistlib, subprocess, `fcntl`, pytest, Ruff, mypy, Alembic, macOS launchd.

## Global Constraints

- One LaunchAgent runs every 14400 seconds; each connector job times out after 1800 seconds.
- Full reconciliation is due independently per `(source, profile_id, account_id)` after 7 days and advances only on `succeeded` full runs.
- The global lock is nonblocking; one failed/timed-out source never prevents later jobs or future scheduled runs.
- Plists/logs/state contain no secret or health value. Child stdout/stderr and exception messages are never relayed.
- Automation data directories are mode `0700`; managed files and the explicit env file are regular non-symlink mode `0600` files written atomically.
- Logs rotate above 5242880 bytes and retain one `.1` generation.
- Tests install no LaunchAgent, perform no OAuth/network/real connector sync, and use injected adapters/runners.
- Telegram is out of scope, but the registry accepts future adapters without runner changes.

---

### Task 1: Discover and run connector jobs safely

**Files:**
- Create: `src/health_agent/automation/__init__.py`
- Create: `src/health_agent/automation/models.py`
- Create: `src/health_agent/automation/storage.py`
- Create: `src/health_agent/automation/registry.py`
- Create: `src/health_agent/automation/runner.py`
- Modify: `src/health_agent/config.py`
- Test: `tests/automation/test_registry.py`
- Test: `tests/automation/test_runner.py`

**Interfaces:**
- Produces: `AutomationJob(source, profile_id, account_id, supports_full, arguments)`, `AutomationResult`, `JobAdapter.discover(settings)`, `configured_job_adapters(settings, executable)`, `AutomationRunner.run(force_full=False)`.
- Produces: `AutomationState.full_due(job, now)`, `mark_full_success(job, now)`, and `GlobalRunLock.acquire()` as private atomic/macOS-safe storage primitives.

- [ ] **Step 1: Write failing registry tests**

Cover database-backed WHOOP connection discovery plus temporary Gmail/Drive profile stores, stable source/profile/account ordering, exact connector arguments, malformed/symlinked local configuration failing only its source, and overlapping identifiers remaining distinct.

- [ ] **Step 2: Write failing runner tests**

Use injected adapters, clock, executor, state, and lock. Assert first/full-due versus incremental modes, failed/deferred full not advancing state, one failure/timeout followed by later success, empty registry success, nonblocking overlap, safe bounded output, and no raw child output/exception text.

- [ ] **Step 3: Implement the immutable contracts and private storage**

Use exact tuple job keys and ISO-8601 UTC checkpoint timestamps. Atomic writes use a same-directory temporary file, `fsync`, `os.replace`, and mode `0600`; reject symlink path components. The lock uses `O_NOFOLLOW` where available and `fcntl.LOCK_EX | LOCK_NB`.

- [ ] **Step 4: Implement production discovery and subprocess execution**

WHOOP selects `WhoopConnection` identities; Gmail/Drive enumerate only validated profile directories through their existing stores. Run argument lists with `shell=False`, captured text, inherited environment plus `HEALTH_AGENT_ENV_FILE`, and `timeout=1800`. Recognize only documented safe status tokens; map every other outcome to fixed codes.

- [ ] **Step 5: Run focused tests and commit**

Run `uv run pytest -q tests/automation/test_registry.py tests/automation/test_runner.py`, `uv run ruff check src/health_agent/automation tests/automation`, and `uv run mypy src`; commit the task.

---

### Task 2: Render and manage the user LaunchAgent

**Files:**
- Create: `src/health_agent/automation/launchd.py`
- Test: `tests/automation/test_launchd.py`

**Interfaces:**
- Produces: `LaunchdPaths.resolve(...)`, `LaunchdManager.render()`, `install()`, `status()`, `stop()`, `remove()`, and `rotate_safe_logs()`.
- Consumes: fixed label `com.orange.health-agent.sync`, interval/timeout/root settings from Task 1, and an injected `Launchctl` command runner.

- [ ] **Step 1: Write failing plist and filesystem tests**

Assert exact absolute executable, working directory, env-file argument, stdout/stderr paths, `StartInterval=14400`, `RunAtLoad=true`, valid plist round-trip, no loaded env values/secrets/database URL, `0700` data directories, `0600` files, atomic replacement, and symlink/non-regular rejection.

- [ ] **Step 2: Write failing lifecycle and log tests**

With an injected launchctl runner, cover render-only, idempotent install/bootstrap, loaded/unloaded status, stop retaining files, remove deleting only exact managed plists, off-macOS failure, and >5 MiB one-generation log rotation preserving mode `0600`.

- [ ] **Step 3: Implement launchd rendering and lifecycle**

Render with `plistlib`; use exact `gui/$UID` domain and service target, argument-list subprocesses, and content-free statuses. Pre-create logs privately before bootstrap. Never chmod user-owned parent directories such as `~/Library/LaunchAgents`.

- [ ] **Step 4: Run focused tests and commit**

Run `uv run pytest -q tests/automation/test_launchd.py`, focused Ruff, and mypy; commit the task.

---

### Task 3: Expose CLI, document operation, and verify the complete slice

**Files:**
- Modify: `src/health_agent/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/runbooks/automation.md`
- Create: `docs/superpowers/reports/2026-09-04-macos-background-automation-report.md`
- Test: `tests/automation/test_cli.py`

**Interfaces:**
- Produces CLI commands: `automation sync`, `render`, `install`, `status`, `stop`, and `remove`, all with explicit `--env-file` where configuration is required.
- Consumes Task 1 runner and Task 2 launchd manager through small factories that tests can replace without process/network/launchd access.

- [ ] **Step 1: Write failing CLI tests**

Assert exact safe output/exit behavior for all commands, absolute env-file validation, failed job summary with later success, already-running success, `install`/`stop`/`remove` lifecycle routing, and that neither fake secrets nor raw child output reach stdout/stderr.

- [ ] **Step 2: Implement thin Typer composition**

Instantiate `Settings(_env_file=...)`, resolve the current installed executable/repository root, run log rotation before sync, and print one bounded line per result plus a final count summary. Exit nonzero after completing all jobs if any failed/timed out; deferred and overlap are truthful nonfatal states.

- [ ] **Step 3: Write concise operator docs and implementation report**

Document setup, four-hour/seven-day behavior, exact commands and paths, stop versus remove, log/state locations, safe status meaning, manual-sync coexistence, timeout behavior, and explicit exclusions (no Telegram daemon, OAuth, or live sync in this slice).

- [ ] **Step 4: Run complete verification**

Run focused automation tests, full `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`, `uv lock --check`, `git diff --check`, disposable `uv run alembic upgrade head && uv run alembic check`, and plist parse/CLI help smoke. Do not run install/OAuth/real sync.

- [ ] **Step 5: Commit and request independent whole-branch review**

Commit only source/tests/docs/local-safe templates. Give the reviewer the complete `74f29c4..HEAD` diff and require SPEC/QUALITY verdicts for discovery, isolation, timeout/non-overlap, checkpoint safety, plist lifecycle, permissions, safe logs, test isolation, and truthful docs.
