# Pausing unused Telegram coaches

Food and training chat processes can be disabled independently of the main bot.
For each installed pilot label, run `launchctl disable gui/UID/LABEL`, then
`launchctl bootout gui/UID/LABEL`. The disable flag persists across login/reboot;
bootout can take a short time to finish. Keep installed plists and private data.
Verify with `launchctl print-disabled gui/UID` and `launchctl print gui/UID/LABEL`.

The labels are `com.orange.health-agent.pilot.food` and
`com.orange.health-agent.pilot.training`. Do not disable
`com.orange.health-agent.telegram` when the main bot should still answer questions.

Automatic sleep questions are controlled by the existing `/утро выкл` command,
which saves a profile-scoped `sleep/schedule` record with `enabled=false`.
The main poller reads this schedule on every cycle; a restart is unnecessary.
Manual questions and `/опрос` remain available. Body reminders and weekly reports
have separate schedules and are not disabled by this command.

## Keep COROS ingestion

The training runtime also contains the COROS daily collection gate. When that
runtime is stopped, use its independent, message-free entry point:

```sh
.venv/bin/health-agent pilot coros-sync --automatic --profile-id PROFILE_UUID
```

It reuses `maybe_coros_sync`: a rolling seven-date window, daily success gate,
one-hour failure backoff and the existing per-account lock. Date bounds cannot be
combined with `--automatic`. `status=checked attempted=false` only means that the
gate skipped work; it does not prove valid credentials or complete data. Inspect
the latest `training/sync_run` for success, incomplete or failed results.

The local job `com.orange.health-agent.coros-sync` is a one-shot hourly LaunchAgent
with RunAtLoad, without KeepAlive. Its working directory is the repository; it
invokes the absolute virtualenv executable and the command above. Its private
logs live under `data/pilot/training/coros-automation/`. No Telegram credentials,
questions or LLM calls are needed by this entry point.

If COROS fails authorization, the independent job retains retry capability but
cannot restore a revoked grant by itself. Do not describe token-file presence as
successful authentication.

## Re-enable

Use `launchctl enable gui/UID/LABEL`, then bootstrap the preserved plist for each
requested bot. Restoring a chat may process queued Telegram updates. Restore
morning questions with `/утро 08:00` or the owner's chosen time.

If the training bot is re-enabled, remove or unload the separate COROS job to
avoid unnecessary duplicate scheduling (the existing account lock and daily
gate still prevent concurrent ingestion). WHOOP, Qingping, weather, database,
generic reminders and the main bot have separate jobs.
