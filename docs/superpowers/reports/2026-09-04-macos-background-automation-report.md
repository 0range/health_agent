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

- Automation-focused tests: 22 passed.
- Full project tests: 461 passed.
- Ruff, mypy, lockfile, diff whitespace, CLI-help, plist parsing, and disposable
  Alembic upgrade/check gates passed.
