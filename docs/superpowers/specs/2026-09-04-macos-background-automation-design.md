# macOS background automation design

## Goal

Run configured WHOOP, Gmail, and Google Drive synchronization automatically on
one local Mac without exposing secrets, mixing profiles, or letting one failed or
hung source stop the others. Telegram remains an extension point until its
application composition is merged.

## User flow

- `health-agent automation sync --env-file /absolute/path/.env` discovers all
  configured source/profile/account targets and runs them sequentially.
- `automation render`, `install`, `status`, `stop`, and `remove` manage one user
  LaunchAgent. Installation is explicit; development/tests never load it.
- The LaunchAgent runs every four hours. The first successful run for each target
  is full; afterward runs are incremental until that target's last successful
  full run is seven days old.
- Output contains only source, profile UUID, account slot, mode, status, and a
  fixed safe error code. Connector stdout/stderr is never relayed.

## Architecture

`automation` is a small composition layer, not another connector. An extensible
job registry has three discovery adapters:

- WHOOP reads configured `WhoopConnection` profile/account identities from
  PostgreSQL;
- Gmail reads profile/account configuration from its private local profile store;
- Drive reads configured profile identities from its private local profile store.

Each discovered immutable job knows how to build the existing connector CLI
command. A command executor runs the exact current `health-agent` executable with
the selected environment file, a 30-minute per-job timeout, captured output, and
no shell. Recognized content-free connector status tokens become automation
results; raw child output and exception text are discarded. One failure or
timeout records a safe result and processing continues.

The whole run takes a nonblocking global `flock`. If another run owns it, the new
run exits successfully with `status=skipped safe_error=already_running`. The lock
is released on every normal exception and timeout. Connector-specific locks
remain authoritative for manual-versus-scheduled overlap.

Per-job full-checkpoint state is an atomic private JSON file. Only `succeeded`
full jobs advance their timestamp; failures, timeouts, and deferred WHOOP runs
remain due. State identity is the exact tuple `(source, profile_id, account_id)`.

## LaunchAgent contract

Label: `com.orange.health-agent.sync`.

The rendered plist contains only:

- the resolved absolute `health-agent` executable;
- `automation sync --env-file` and the resolved absolute environment-file path;
- the resolved repository working directory;
- `StartInterval=14400`, `RunAtLoad=true`, and background process metadata;
- absolute stdout/stderr paths under ignored `data/automation/logs`.

It contains no token, password, OAuth credential, database URL, or expanded
environment-file value. Rendered/installed plists, state, locks, and logs are
atomic/private local artifacts: managed directories are `0700`, files are
`0600`, and symlink/non-regular targets fail closed. The explicit environment
file must be a regular non-symlink `0600` file. LaunchAgent writes are atomic.

`render` writes the inspectable plist under `data/automation/launchd`. `install`
copies it to `~/Library/LaunchAgents` and runs `launchctl bootstrap gui/$UID`;
`status` uses `launchctl print`; `stop` uses `bootout` but retains files;
`remove` stops and deletes only the exact managed plist files. Launchctl actions
fail clearly off macOS and are injectable in tests.

Before each run, the two safe LaunchAgent log files rotate when larger than 5 MiB;
one `.1` generation is retained. The automation process emits only bounded
single-line results, so logs contain no connector response bodies or health
values.

## Failure semantics

- Discovery failure for one source produces one safe source-level failure and
  does not suppress jobs from other sources.
- A nonzero connector exit, unknown child status, spawn error, or timeout maps to
  a fixed code. No child stdout/stderr or exception message is logged.
- State persistence failure is reported safely and makes a successful full remain
  due rather than claiming a checkpoint that was not durably written.
- Empty configuration is a successful run with zero jobs.
- A future Telegram daemon adapter can register another job kind without changing
  runner/lock/checkpoint/launchd code.

## Verification

Tests use injected discovery adapters, command executors, clocks, locks, and
launchctl runners; they perform no network/OAuth/real sync and install no
LaunchAgent. Coverage includes multi-profile/account discovery, stable ordering,
one failure and one timeout followed by later success, non-overlap, seven-day
full scheduling, failed-full retry, safe output, log rotation, exact plist fields,
permissions/symlink rejection, and lifecycle command behavior. Final gates are
focused tests, full pytest, Ruff, mypy, Alembic upgrade/check on disposable
PostgreSQL, and plist syntax validation.
