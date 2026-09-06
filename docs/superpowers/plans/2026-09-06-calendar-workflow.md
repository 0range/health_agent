# Calendar workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make reviewed Calendar adapter usable from visits, Telegram, panel and regular automation.
**Architecture:** One publication row per profile-bound visit, explicit opt-in, post-commit synchronization and the existing automation runner. Local visit is source of truth.
**Tech Stack:** Existing Python/SQLAlchemy/PostgreSQL/Typer/Telegram/panel/httpx.

## Global Constraints

- No live OAuth flow or Calendar event writes during this task.
- No invitation emails. Explicit publication only; unchosen local visits never cause external calls.
- Local writes must commit before any network request; failures preserve local changes and report queued/failed truthfully.
- Profile identity must be validated against PostgreSQL before config/token writes.
- Do not add frameworks, another scheduler or unbounded scans. No credentials, medical text or raw HTTP errors in logs.
- User explicitly requested autonomous parallel completion; do not ask for plan approval or user tests now.
- Own schema revision `0012_visit_calendar`, down_revision `0011_medical_workflows`; another parallel task will own a separate branch revision and root merges heads later.

### Task 1: Connect explicit visit publication end to end

**Files:** create `src/health_agent/google_calendar/composition.py`, `publication.py`, schema revision `alembic/versions/0012_visit_calendar.py`, covering tests under `tests/google_calendar/`; modify existing config/CLI/automation models+registry, visits Telegram, panel workflow/rendering/composition, docs/google-calendar.md. Keep changes narrowly scoped; root owns separate dashboard and PDF integrations.

**Interfaces:** consume existing CalendarService.sync(CalendarEvent), CalendarOAuth, stores, VisitRepository get/notes and scoped session factories. Produce `build_calendar_service(settings)`, `CalendarPublicationService(engine, calendar_service, lock_root)` with `publish(profile_id, code)` (opt in and attempt), `sync_visit(profile_id, code, *, only_if_opted_in=True)` and `sync_profile(profile_id, limit=100)`; safe snapshot/status for panel and tests. Public return immutable result indicating published/unchanged/queued with safe error and optional validated Google URL. Existing adapter statuses map explicitly.

- [ ] Step1: add failing synthetic DB tests. Use repository.create with an explicit owner; assert `sync_visit(owner,code)` before opt-in yields no fake gateway calls. `publish(owner,code)` repeated yields one opt-in and same event ID; `publish(other,code)` fails with no writes/calls. A fake callback opening a second session must see committed notes. Timeout records queued while persisted note remains.

```python
assert fake.calls == []
service.sync_visit(owner, code)
assert fake.calls == []
service.publish(owner, code)
service.publish(owner, code)
assert {event.visit_id for event in fake.calls} == {visit.id}
```

- [ ] Step2: implement a publication ORM table with composite FK `(visit_id,profile_id)` to health_visits and unique/primary key ensuring one opt-in per visit. Fields include successful content fingerprint, attempted_at/synced_at, safe status/error, validated link. Add migration and metadata imports. Preserve existing visit snapshots, no irrelevant schema refactor. Build CalendarEvent from fresh local title/times/status and first20 question notes, bounded1000chars each with clear truncation note; never send answer notes. Serialize same-visit sync via private file lock; persist fingerprint of exact sent snapshot, so concurrent DB edits remain different next cycle. Do not hold SQL transaction across network. Do not overwrite another profile/account/calendar target silently.

```python
with sessions() as session:
    snapshot = read_owned_snapshot(session, profile_id, code)
# The transaction has committed before gateway use.
result = calendar.sync(snapshot.event)
with sessions() as session:
    record_exact_attempt(session, snapshot, result)
```

- [ ] Step3: wire Settings `GOOGLE_CALENDAR_ROOT` default data/google-calendar and client-secrets default data/secrets/google-oauth-client.json. Register factory CLI with DB profile validator, add sync command and visit publication command. Add `/visit_calendar CODE` plus help, ensure other visit edits invoke opted-in sync only after commit; failures appended as safe queued notice. Add matching panel POST action and publication status with existing CSRF/origin/size protection; GET must be read-only. Do not present an authorization-free event as actually in Calendar.
- [ ] Step4: add Calendar automation source and discovery from configured profiles/opt-in data, bounded per-profile sync. Missing authorization is deferred with safe code; no surprise browser. Show Calendar connection and publication backlog on medical page and healthcheck using existing local state. Harden staging config so Calendar roots/secrets cannot fall back to production.
- [ ] Step5: test CLI profile guard, panel CSRF/foreign-code/GET-no-write, Telegram routing/publish/edit/move/cancel, queued retry, edited-during-sync then converged, duplicate delivery, missing OAuth, safe URL/exception handling, automation command construction and staging isolation. Run focused new suites plus existing visits/panel/automation/staging tests, Ruff and mypy; append exact evidence to task report and commit. No full suite necessary before independent review.
