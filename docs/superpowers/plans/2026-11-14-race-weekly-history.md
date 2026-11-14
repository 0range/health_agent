# Race Weekly History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> This session executes the already authorized configuration change inline;
> no subagent delegation or extra approval is needed.

**Goal:** Preserve registration history and send a forecast table every Sunday at 13:00 Moscow through Training bot.

**Architecture:** Extend the existing daily ChatGPT task's instructions. Keep
the sourced baseline in the repository and embed the same dates in its prompt.
Railway remains the existing delivery relay.

**Tech Stack:** Markdown evidence, authenticated Scheduled task editor, existing HTTPS training relay.

## Global Constraints

- Season 2027; all individual Running Community 5/10 km starts, IRONSTAR 1/8 only Sochi/Sirius and Luzhniki.
- Sunday at 13:00 Europe/Moscow replaces that day's ordinary status.
- Dates inferred from history are estimates, not organizer announcements.
- Preserve training routing, credentials, email fallback and current daily cadence.
- Do not change security task or install a local scheduler.
- Never put sender credentials into source control or diagnostic output.

### Task 1: Preserve the historical baseline

**Files:** Create `docs/research/race-registration-history-2027.md`.

**Interfaces:** Consumes dated public organizer posts and event fee periods;
produces a sourced table of 2025/2026 openings, season-relative 2027 estimates,
observed spread, confidence limitations and sellout evidence.

- [ ] Record the ten event groups with links and explicit unknown observations.
- [ ] Compute forecast offsets with `opening.year + 2027 - season`; check that
  White Nights 2025 projects to November 2026 and Luzhniki 2026 to December 2026.
- [ ] Keep the Sirius festival proxy distinct from its unverified 1/8 opening.

### Task 2: Update and verify the cloud task

**Files:** Modify `docs/runbooks/race-monitor.md`; private task prompt only in the authenticated editor.

**Interfaces:** Consumes Task 1's baseline; produces a Sunday table in the same
daily task and delivery path, with no second Sunday message.

- [ ] Preserve a mode-0600 temporary backup of the existing task.
- [ ] Add Sunday-only table instructions, countdown rules, source-error handling,
  historical dates/URLs and sellout precision to the existing prompt.
- [ ] Save through the task editor and read back enabled state, prompt markers,
  default timezone and next run times; correct any changed 13:00 Moscow timing.
- [ ] Update the runbook with the observed saved configuration and limits.
- [ ] Delete private temporary backups after successful verification.
- [ ] Review `git diff --check`, confirm no credentials entered the diff, and
  commit only the task's documentation changes.

### Task 3: Authorized frequent cloud checks

**Files:** Create `src/health_agent/races/cloud.py`, `race-windows-2027.json`,
`deploy/races.Dockerfile`, `tests/races/test_cloud.py`; update the runbook.

**Interfaces:** Reuse `collect(config, fetch) -> (observations, errors)` and
`Observation.registration_url`. Expose authenticated `GET /status` and
`POST /openings` for the daily task. Persist JSON state and immutable outbox in
`/data`; send through the existing training relay only.

- [ ] Test cadence boundaries, year rollover, announced-opening overrides,
  stale announcements, direct-link requirements and restart-safe deduplication.
- [ ] Implement a bounded worker loop, official-source fetch allowlist,
  explicit announcement parser and authenticated metadata endpoints.
- [ ] Run targeted race tests and a read-only live check; inspect failures.
- [ ] Create an isolated Railway service and volume, configure training-only
  relay access, deploy the reviewed source commit, and inspect health/status.
- [ ] Link the daily task to worker receipts and announcement configuration;
  verify daily 13:00 Moscow and Sunday instructions on readback.
- [ ] Verify actual cloud checks and one clearly labeled setup message through
  Training bot. Record receipt and limitations; remove private local backups.
