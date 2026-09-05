# Always-on Telegram LaunchAgent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run the existing Telegram long-poller continuously as an isolated, restart-safe macOS user LaunchAgent.

**Architecture:** Add a Telegram-specific plist/lifecycle manager that reuses private filesystem and lock primitives. A hidden CLI wrapper holds the Telegram-only lock, rotates safe logs, and starts the existing `telegram run` command as a child with the selected private env file.

**Tech Stack:** Python 3.13, Typer, plistlib, launchctl, subprocess, fcntl, pytest.

## Global Constraints

- Label is exactly `com.orange.health-agent.telegram`, separate from sync and reminder labels.
- Plist uses absolute paths, `RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=30`, and no secrets or health text.
- Environment file is absolute, regular, non-symlink and exactly `0600`.
- Managed directories are `0700`; plist/log/lock files are `0600`; logs retain at most one generation above 5 MiB.
- Rotation occurs only under the Telegram singleton lock.
- Wrapper invokes the existing `telegram run`, passes it the held lock descriptor,
  and does not change question/capture composition.
- Tests must not call live launchd, Telegram, OpenAI, OAuth, or real credentials.

---

### Task 1: Telegram-specific LaunchAgent lifecycle

**Files:**
- Create: `src/health_agent/telegram/launchd.py`
- Create: `tests/telegram/test_launchd.py`

**Interfaces:**
- Consumes: `atomic_private_write`, `private_directory`, `reject_symlink_components`, `require_private_file`, and `GlobalRunLock` from `health_agent.automation.storage`.
- Produces: `TELEGRAM_LABEL`, `TelegramLaunchdPaths.resolve(...)`, `TelegramLaunchdManager.render/install/status/stop/remove`, and `rotate_telegram_logs(paths)`.

- [ ] Write failing tests that parse the exact plist, assert distinct label/path ownership, modes and no env content, and exercise fake-launchctl install/idempotency/reload/rollback/stop/remove.

```python
payload = plistlib.loads(manager.render().read_bytes())
assert payload["ProgramArguments"] == [str(executable), "telegram", "service-run", "--env-file", str(env_file)]
assert payload["KeepAlive"] is True
assert payload["ThrottleInterval"] == 30
assert TELEGRAM_LABEL not in {LABEL, REMINDER_LABEL}
```

- [ ] Run `uv run --offline pytest -q tests/telegram/test_launchd.py` and confirm collection fails because the module is absent.
- [ ] Implement absolute path resolution, exact secret-free plist rendering, atomic private installed writes, exact-label launchctl lifecycle, changed-config rollback, lock-serialized log rotation, and symlink/non-regular rejection.
- [ ] Add boundary tests for relative/public/symlink env files, hostile managed targets, lock contention, and one-generation rotation.
- [ ] Run the focused tests, `uv run --offline ruff check src/health_agent/telegram/launchd.py tests/telegram/test_launchd.py`, and `uv run --offline mypy src`; commit.

### Task 2: CLI wrapper, runbook, and complete gates

**Files:**
- Modify: `src/health_agent/cli.py`
- Create: `tests/telegram/test_launchd_cli.py`
- Modify: `docs/integrations/telegram.md`
- Create: `docs/superpowers/reports/2026-09-05-telegram-launchd-report.md`

**Interfaces:**
- Consumes: Task 1 paths, manager, rotation and the existing `_current_console_script()`.
- Produces: `telegram render/install/automation-status/stop/remove --env-file PATH` and hidden `telegram service-run --env-file PATH`.

- [ ] Write failing CLI tests using fake manager, fake lock, and fake subprocess. Verify lifecycle routing, exact safe output, no live launchctl, singleton skip, private env validation, child arguments/cwd/env, lock release, and child failure propagation.

```python
result = runner.invoke(app, ["telegram", "service-run", "--env-file", str(env_file)])
assert child.arguments == (str(executable), "telegram", "run")
assert child.environment["HEALTH_AGENT_ENV_FILE"] == str(env_file)
assert "SECRET" not in result.output
```

- [ ] Run `uv run --offline pytest -q tests/telegram/test_launchd_cli.py` and confirm the commands are absent.
- [ ] Implement thin safe CLI composition. Pass an explicit child environment and cwd with `shell=False`; hold/release the singleton lock around rotation and the entire child lifetime. Map configuration, launchctl and child failures to bounded content-free messages.
- [ ] Update the Telegram runbook with the five operator commands, owned files, restart/stop semantics, live-only validation, and distinction from connector/reminder LaunchAgents.
- [ ] Run focused Telegram/automation/reminder tests, then full `uv run --offline pytest -q`, `uv run --offline ruff check .`, `uv run --offline mypy src`, `uv lock --check`, `git diff --check`, CLI help and plist parse smokes. Do not run install or the service wrapper outside fakes.
- [ ] Write the implementation report, generate `.superpowers/sdd/2026-09-05-telegram-launchd/review-18d97a3..<HEAD>.diff`, self-review, and commit the final documentation/package.

### Task 3: Serialize lifecycle and verify rollback recovery

**Files:**
- Modify: `src/health_agent/telegram/launchd.py`
- Modify: `tests/telegram/test_launchd.py`
- Modify: `tests/telegram/test_launchd_cli.py`
- Modify: `docs/integrations/telegram.md`
- Modify: `docs/superpowers/reports/2026-09-05-telegram-launchd-report.md`

**Interfaces:**
- Consumes: `GlobalRunLock` and the existing manager private lifecycle helpers.
- Produces: `TelegramLaunchdError.safe_code`,
  `TelegramLaunchdError.previous_service_restored`, and a distinct
  `telegram-lifecycle.lock` path owned only by this manager.

- [ ] Add a failing double-bootstrap test whose new bootstrap and rollback
  bootstrap both return non-zero. Assert the old plist bytes are restored but
  the exception has safe code `launchctl_rollback_bootstrap_failed` and
  `previous_service_restored is False`; retain the successful-rollback test with
  `previous_service_restored is True`.
- [ ] Add a failing deterministic concurrent-install test. Pause the winning
  manager inside its lifecycle transaction, invoke a second manager, assert the
  loser receives `telegram_lifecycle_busy`, then release the winner and prove its
  installed plist remains present and byte-identical.
- [ ] Add the minimal Telegram-only lifecycle lock and locked private helpers.
  Hold it across render/read/write/print/bootout/bootstrap/rollback for install,
  and across every read/write/launchctl step for render, status, stop, and remove.
  Never reuse the poller singleton lock and never nest lock acquisition.
- [ ] Map lifecycle failures through the existing bounded CLI boundary without
  exposing exception causes, launchctl output, paths, env values, or health text.
  Add a CLI regression for the distinct rollback code.
- [ ] Run the focused tests, then full pytest, Ruff, mypy `src`, lock check,
  Alembic-head check and diff check. Update the report and review package without
  invoking live launchd or Telegram; commit the complete hardening change.
