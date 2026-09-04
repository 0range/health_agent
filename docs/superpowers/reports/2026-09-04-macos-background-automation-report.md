# macOS background automation implementation report

## Delivered

- One extensible, stable-ordered registry discovers WHOOP, Gmail, and Drive
  targets without combining profile/account identities.
- A nonblocking global lock and sequential runner isolate discovery, process,
  timeout, and state-write failures while continuing later jobs.
- Private atomic state tracks successful full reconciliation independently per
  source/profile/account and schedules it every seven days.
- A secret-free LaunchAgent runs every 14 400 seconds with a 1 800-second job
  timeout, bounded private logs, and explicit render/install/status/stop/remove
  lifecycle.
- Thin CLI commands expose only content-free results and require an absolute,
  regular, non-symlink `0600` environment file.

## Verification boundary

Tests inject discovery, executors, clocks, locks, subprocess results, and
launchctl. They perform no OAuth, network access, real connector sync, or
LaunchAgent installation. Live installation remains an explicit operator step.

## Verification

- Automation-focused tests: 32 passed.
- Full project tests: 584 passed.
- Ruff, mypy, lockfile, diff whitespace, CLI-help, plist parsing, and disposable
  Alembic upgrade/check gates passed.

## Review fixes

- Console-script resolution no longer depends on `PATH`; a subprocess regression
  invokes the actual installed script with launchd's minimal path.
- Reinstalling changed configuration reloads an already loaded service and
  restores the previous plist if replacement activation fails.
- Log rotation runs only after the global lock is acquired, so an overlapping
  process returns `already_running` without touching log files.

## Live-readiness correction

- An absent Gmail root or a connector-created profile directory without a saved
  Gmail profile is treated as no configured job, while malformed/symlinked saved
  configuration still fails closed.
- A configured Gmail account or Drive profile without its first verified OAuth
  token is emitted as `deferred/oauth_not_ready`, does not invoke the connector,
  does not advance the full checkpoint, and does not make the unattended run
  exit nonzero.
- Existing or malformed tokens still reach normal connector validation, so
  genuine OAuth and connector failures remain failures.
