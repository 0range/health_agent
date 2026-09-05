# Always-on Telegram LaunchAgent v0.1 — implementation report

Branch: `codex/v1-telegram-launchd`, base `18d97a3`.

## Delivered

- Dedicated `com.orange.health-agent.telegram` user LaunchAgent with
  `RunAtLoad`, unconditional `KeepAlive`, 30-second launchd throttle, restrictive
  umask, and absolute executable/repository/env/log paths.
- `telegram render`, `install`, `automation-status`, `stop`, and `remove` manage
  only the Telegram plist. Changed loaded configuration is reloaded with previous
  plist/service rollback on failure; non-macOS and ambiguous launchctl status fail
  with content-free errors.
- Every plist and launchctl lifecycle transaction is protected by a dedicated
  per-user lock shared across env files and automation roots. Concurrent
  lifecycle commands fail safely before reading, writing, unloading, restoring,
  or deleting the winner's managed plist.
- Failed new bootstrap reports whether the previous service was actually
  restored. A failed rollback bootstrap uses the distinct bounded
  `launchctl_rollback_bootstrap_failed` code with
  `previous_service_restored=false`; exception causes and the bool metadata are
  not printed by the CLI.
- Hidden `telegram service-run --env-file ABS` wrapper validates the private env,
  takes a Telegram-only lock, rotates private logs, and runs the unchanged
  existing `telegram run` using a minimal child environment. The child inherits
  the lock descriptor, preventing an orphan child from overlapping a restart.
- Active logs are reopened explicitly after rotation instead of inheriting the
  pre-rotation launchd descriptor. Rotation retains one generation above 5 MiB,
  refuses symlink/non-regular targets, and cannot race the running poller.
- Installed/rendered plists, logs and lock are `0600`; managed roots are `0700`.
  Plists contain no env values, token, health text, or webhook configuration.
- Telegram long-poll composition, private binding, question/capture behavior and
  its existing startup webhook refusal were not changed.

## Verification

All tests used fake launchctl/subprocess adapters, synthetic private files,
temporary homes and the disposable test database. No real LaunchAgent, bot,
Telegram/OpenAI request, token, OAuth, or personal health data was touched.

| Gate | Result |
| --- | --- |
| Focused launchd/CLI tests | 21 passed |
| Related Telegram/automation/reminder regressions | 64 passed before final hardening |
| `uv run --offline pytest -q` | 653 passed, five existing SWIG warnings |
| `uv run --offline ruff check .` | PASS |
| `uv run --offline mypy src` | PASS |
| mypy `src` plus changed tests | PASS |
| `uv lock --check` | PASS |
| `uv run --offline alembic heads` | one head, `0006_health_reminders` |
| `git diff --check 18d97a3..HEAD` | PASS |
| `health-agent telegram --help` | five lifecycle commands registered; internal wrapper hidden |

The deterministic concurrent-install regression uses two env configurations
with different automation roots, pauses the lock owner at bootstrap, and proves
the losing install cannot render, replace, or delete the winner's plists. The
double-bootstrap regression verifies old plist restoration is not confused with
successful old-service recovery.

## Live-only handoff

After integration, the owner must use a test bot/chat for the first live smoke:
run `telegram run` once, stop it, render and inspect the plist, install it, and
check both `telegram automation-status` and the existing heartbeat-oriented
`telegram status`. Stop/remove semantics were tested only through fake launchctl.
No merge, push or live installation was performed here.
