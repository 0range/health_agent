# Always-on Telegram LaunchAgent v0.1

Date: September 5, 2026. This slice makes the existing `health-agent telegram
run` long-poller an explicit, always-on macOS user service. It does not change
question answering, capture, Telegram authentication, or webhook policy.

## Chosen architecture

Use a dedicated `com.orange.health-agent.telegram` LaunchAgent rather than the
four-hour connector or one-minute reminder jobs. A narrow Telegram launchd
module owns its plist, logs, lock, and lifecycle. It reuses the existing private
filesystem and advisory-lock primitives but cannot start, stop, or remove the
other managed labels.

The plist invokes a hidden `telegram service-run --env-file ABSOLUTE_PATH`
wrapper through the absolute installed `health-agent` console script. The
wrapper validates the private `0600` non-symlink environment file, takes a
Telegram-only non-blocking lock, safely rotates the two logs, and launches the
existing `telegram run` command with `HEALTH_AGENT_ENV_FILE` set only in the
child environment. It holds the lock for the child lifetime and returns the
child exit code. No credential or health content enters arguments or plist.

## LaunchAgent and lifecycle contract

The plist contains only absolute executable, repository, environment-file and
log paths. It uses `RunAtLoad=true`, `KeepAlive=true`, `ThrottleInterval=30`,
`ProcessType=Background`, and `Umask=63`. `telegram run` already performs
`getMe` identity validation and refuses configured webhooks; this slice opens no
listener and does not weaken its bound-private-chat routing.

`telegram render`, `install`, `automation-status`, `stop`, and `remove` require
an absolute private `--env-file`. Render is inspectable and never loads a job.
Install is idempotent, reloads changed configuration, and restores the previous
plist/service if reload fails. Stop unloads the exact Telegram label while
retaining files; launchd therefore does not restart it after an intentional
stop. Remove unloads and deletes only the Telegram rendered/installed plists.
Status reports only `loaded` or `unloaded` plus the fixed label.

Rendered roots are `0700`; plist, lock, active logs and one rotated generation
are regular `0600` files. Logs rotate above 5 MiB only while the service lock is
held, so rotation cannot race a running poller. Runtime output remains the
existing bounded content-free status/error lines. Symlink or non-regular
managed targets fail closed.

## Verification and exclusions

Tests parse the plist and use fake launchctl/subprocess adapters, temporary
homes, private synthetic env files, and lock contention. They cover label
isolation, absolute paths, no secret leakage, KeepAlive/backoff, idempotent
install/reload/rollback, stop/remove, private rotation, single instance, safe
child environment and exit propagation. Tests never load launchd, call Telegram,
read real credentials, or run the long-poller. Live install and delivery remain
an explicit owner smoke test after merge.
