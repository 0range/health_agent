# Confirmed health reminders — design

Date: 5 September 2026

Status: approved for implementation as scenario 3 of the Personal Health Agent v0.1

## Outcome

A profile-scoped health suggestion becomes a durable proposal, but cannot become
active until the bound user explicitly confirms it in Telegram. A confirmed
reminder is delivered proactively at its due time and can then be snoozed,
rescheduled, completed, or cancelled. Every proposal keeps a human-readable
reason and source reference.

## Chosen approach

PostgreSQL is the source of truth for reminder lifecycle and audit events. A
small one-shot dispatcher is invoked every minute by a separate user LaunchAgent
and sends through the existing `TelegramMessenger` using deterministic delivery
keys. This gives prompt delivery, replay safety, and restart recovery without a
public server or a long-lived scheduler process.

Two alternatives were rejected:

- running reminders only in the four-hour connector automation would deliver too
  late;
- running only inside Telegram long polling would couple inbound availability to
  proactive delivery and provide no independent restart boundary.

## Data and invariants

`health_reminders` stores `profile_id`, a non-guessable public command code,
title, reason, source type/reference, UTC due time, the explicit IANA timezone,
status, confirmation/delivery/terminal timestamps, and a delivery revision.
`health_reminder_events` stores append-only lifecycle provenance.

Statuses are `pending_confirmation`, `scheduled`, `completed`, and `cancelled`.
Database constraints enforce that a scheduled or completed reminder has a
confirmation timestamp, that completion/cancellation timestamps match their
terminal status, and that revisions remain positive. The dispatcher additionally
selects only `scheduled` rows with `confirmed_at IS NOT NULL`.

All reads and mutations require both `profile_id` and the public code. Telegram
never accepts a profile identifier from message text: it uses the already-bound
private identity. A command from one profile therefore cannot see or mutate
another profile's reminder.

## Telegram flow

The proposal message contains the title, local due time, reason, source, and
exact `/reminder_confirm CODE` and `/reminder_cancel CODE` commands. Confirmation
activates the reminder without an LLM call. Unknown, malformed, stale, or
cross-profile codes return a generic safe response.

At delivery, the message includes exact commands to complete, cancel, snooze by
a bounded duration, or reschedule using a local ISO date/time. Snooze and
reschedule clear the previous delivery timestamp and increment the revision, so
the next occurrence gets a new delivery key. Repeating the same command is safe:
terminal and already-confirmed transitions are idempotent, while incompatible
transitions are rejected without changing state.

## Delivery and failures

Proposal keys are `health-reminder:proposal:<id>` and occurrence keys are
`health-reminder:due:<id>:<revision>`. If Telegram succeeds but PostgreSQL is not
updated before a crash, the next run receives `previously_sent` from
`TelegramMessenger` and safely completes the database acknowledgement. A
Telegram deferred/unknown result is left unresolved for the existing transport
policy; no second message with a different key is generated.

Each reminder is processed independently. A missing Telegram binding or one
delivery failure is reported as a content-free code and does not prevent other
profiles from being processed. The dispatcher uses a non-blocking local global
lock to prevent overlapping scheduled/manual runs.

## Time rules

Every reminder stores a validated IANA timezone; `Europe/Moscow` is the CLI
default. Naive local date/time input is interpreted only in that explicit zone,
converted to UTC for storage, and rejected when it is ambiguous or nonexistent
during a daylight-saving transition. Aware input is accepted only when its
offset matches the chosen zone at that instant. Telegram output is rendered in
the stored zone.

## Local operation

`health-agent reminder propose/list/status/dispatch` provide an inspectable CLI.
Lifecycle commands are also available locally for recovery. `reminder render`,
`install`, `automation-status`, `stop`, and `remove` manage only
`com.orange.health-agent.reminders`; the plist contains paths, never secrets, and
runs `reminder dispatch --env-file <absolute private env>` every 60 seconds.

## Test boundary

Unit tests cover time parsing, state transitions, formatting, and command
parsing. Component tests cover idempotent proposal/due delivery, retry after a
simulated restart, failure isolation, and cross-profile denial. Disposable
PostgreSQL tests apply the Alembic migration and prove database constraints.
LaunchAgent tests parse the rendered plist without installing it. Tests never
use a live database, Telegram token, network, or production files.

