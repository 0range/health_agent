# Daily GPT Race Review Implementation Plan

> Execute inline in this session under the owner's existing authorization.

**Goal:** GPT once daily at 13:00 Moscow, ordinary code for guaranteed and frequent checks.

**Architecture:** Extend the existing worker with a pure clock scheduler using
persisted `checked_at`. Deploy it before editing the existing cloud GPT task.

**Tech Stack:** Python 3.13, zoneinfo, pytest, Railway, existing ChatGPT automation.

## Global Constraints

- Europe/Moscow; fixed worker hours 08:00, 13:00, 18:00; cutoff 2027-12-31.
- Keep hourly and five-minute cadence, delivery receipts and owner-only routes.
- No additional services or dependencies, no security/coaching changes.

### Task 1: Worker clock scheduling

Files: create `src/health_agent/races/schedule.py` and
`tests/races/test_schedule.py`; modify `cloud.py`, `tests/races/test_cloud.py`
and `deploy/races.Dockerfile`.

Interface: `next_scheduled_check(now: datetime) -> datetime` returns the next
strictly future slot. `next_check(now: datetime, previous: datetime | None,
period: int) -> datetime` returns now when a check is due, otherwise the earlier
of the next fixed slot and previous + cadence. All output uses Europe/Moscow.

- [x] Add boundary cases including this required missed-slot assertion:
  `assert next_check(at('2026-11-16T13:00'), at('2026-11-16T12:59'), 86400) == at('2026-11-16T13:00')`.
- [x] Run `.venv/bin/pytest tests/races/test_schedule.py -q` and confirm missing implementation.
- [x] Compute yesterday/today/tomorrow slots with `now.astimezone(MOSCOW).replace(hour=h, minute=0, second=0, microsecond=0) + timedelta(days=offset)`; compare previous with latest due slot; otherwise return `max(now, min(next_scheduled_check(now), previous + timedelta(seconds=period)))`.
- [x] Integrate helper into `Worker.next_check_at(now)` including season cutoff;
  use it in the loop and `/status`. Add next scheduled check and fixed hours to status.
- [x] Include schedule.py in Docker COPY; verify restart checkpoint and one scan at the fixed boundary through worker tests.
- [x] Run `.venv/bin/pytest tests/races -q` and `.venv/bin/ruff check src/health_agent/races tests/races`.
- [x] Update runbooks, commit and push the existing branch. Verify live `/status`
  exposes fixed hours and next due time, deployment SUCCESS and healthy service.

### Task 2: Reduce GPT automation

- [x] Preserve the existing private task snapshot; edit only scheduling language
  and check coordination in its prompt, retaining historical sources and all
  delivery/silence rules. GPT first GETs status, waits up to 60 seconds for the
  regular scan, then queues only if checked_at still predates today's 13:00 slot.
- [x] Save prompt through the authenticated task editor without printing its key.
- [x] In the source chat, update existing task
  `Automation_ef35471305c4819181e796af6d6a2e2a` to the exact VEVENT:

```text
BEGIN:VEVENT
DTSTART;TZID=Europe/Moscow:20261116T130000
RRULE:FREQ=DAILY;BYHOUR=13;BYMINUTE=0;BYSECOND=0;UNTIL=20271231T205959Z
END:VEVENT
```

- [x] Read saved task independently and assert exact prompt, RRULE, enabled state,
  preserved executor/notifications/email settings and next run at 13:00 Moscow.
- [x] Record verification in runbook and commit; delete temporary private snapshots.
- [x] Explain daily GPT/channel coverage and possible Nov–Dec 2026 openings,
  explicitly separating historical estimates from confirmed announcements.
