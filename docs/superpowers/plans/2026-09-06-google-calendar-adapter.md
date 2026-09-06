# Google Calendar Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Publish selected doctor visits with questions to the owner's calendar without duplicate events or mixed user accounts.

**Architecture:** Separate profile-scoped OAuth/token/config store and small Google Calendar client. Visit model stays in PostgreSQL; this adapter consumes an immutable event DTO. Root wires visits/CLI/panel after integration.

**Tech Stack:** Existing google-auth/google-auth-oauthlib and HTTP client dependencies; no new cloud service, queue framework or DB migration.

## Global Constraints

- No live credentials, OAuth browser, network calls or calendar writes in implementation/review. Use injected fakes/synthetic data.
- No changes to Drive/Gmail/Sheets authorizations, no attendees/invitations, no arbitrary URLs, no logging secrets/events/exception bodies.
- Per-profile verified Google identity; exact bounded scopes and private local files. Wrong-profile/account access fails before event writes.
- Only new google_calendar package/tests/docs; do not modify shared config.py, cli.py, panel, visits or automation. Root integration later. User expressly requested parallel isolated autonomous work.

### Task 1: Authenticated idempotent event adapter

**Files:** create `src/health_agent/google_calendar/{__init__,models,stores,oauth,api,service,cli}.py`, `tests/google_calendar/`, `docs/google-calendar.md`.

**Contracts:** immutable `CalendarEvent(profile_id:UUID, visit_id:UUID, title:str, starts_at:datetime, ends_at:datetime, timezone_name:str, questions:tuple[str,...]=(), cancelled:bool=False)` with validation (title1..200, max20questions each1..1000; dates aware/end>start and valid IANA zone). `CalendarProfile(profile_id, calendar_id='primary', account_subject=None, account_email=None, enabled=False)`; validate IDs bounded and URL-encode calendar path segment, never interpret them as URLs. `CalendarResult(event_id, status, html_link=None, safe_error=None)` statuses created/updated/unchanged/cancelled/deferred. Keep safe errors enumerated; no raw HTTP exception crossing boundary.

Store roots passed explicitly, no Settings edit: private connector root and separate token root. UUID-only profile directory; regular0600files,0700dirs, no symlink traversal, atomic write. Reuse existing low-level private-file helpers where compatible, not entire Sheets domain types. Verified token envelope has profile_id, account_subject, account_email, credentials. Exclusive publish lock forbids the same Google subject under a different profile or changing a bound profile's subject silently. Store scoped config separately; status must not read token content or call network unnecessarily. Missing/invalid credential reports local missing/reauth_required safely.

OAuth uses InstalledAppFlow/local loopback (interactive only by explicit flag), exact scopes `openid`, `https://www.googleapis.com/auth/userinfo.email`, `https://www.googleapis.com/auth/calendar.events.owned`; reject unrelated wider scopes, handle Google's equivalent email scope representation explicitly. Bounded refresh; token refresh persisted without losing identity binding. Verify subject and verified email using fixed `https://openidconnect.googleapis.com/v1/userinfo` before publishing credentials. No reuse/overwrite of Sheets/Gmail/Drive token files. Constructor parameters `(client_secrets:Path, profiles:CalendarProfileStore, tokens:CalendarTokenStore, gateway_factory)`; expose authorize(profile_id,interactive=False,force=False), load/stage/local_status consistent with existing connectors. Profile must already exist/configured; root service checks actual DB profile before auth command.

API uses fixed `https://www.googleapis.com/calendar/v3`, bounded timeout30s, redirects off, no hidden write retries. Stable event ID = SHA256 hex of `health-agent-visit-v1:{profile_id}:{visit_id}` (64 chars, fits base32hex alphabet). Store profile/visit identity as private extendedProperties. No attendees, conferenceData, attachment links, or notifications (`sendUpdates=none`); visibility private. Description contains bounded visit questions and no complete medical archive. RFC3339 start/end with timezone. Escape source question text for HTML interpretation rather than allowing markup injection.

`CalendarService(profiles,tokens,oauth,gateway_factory).sync(event)` first checks matching enabled profile+bound credential identity. GET stable event ID. Missing planned event→POST same deterministicID. POST409→GET existing event and accept only matching private ownership; leave changed content for next explicit sync or update safely with observed ETag. Existing owned event→PATCH only managed fields with If-Match ETag; unknown ownership, existing attendees or mismatched IDs fail closed with no patch. Identical managed fields→unchanged. Cancelled local event: never create it; if owned remote exists, PATCH status=cancelled (no DELETE), absence/alreadycancelled→unchanged. Unknown write outcome returns safe deferred; next explicit sync reconciles GET same ID, never invents another ID. 401/403/429/5xx map to safe auth/permission/rate/unavailable codes; no raw body output. No write under an open DB transaction (this adapter owns no DB).

CLI exported Typer app with configure/status/authorize, explicit --profile-id; paths from existing Settings roots or constructor injection without new Settings fields (root will provide composition). Do not add a user-triggerable publish command with synthetic/unchecked profile; root visits integration owns sync. If clean standalone CLI composition requires shared Settings, export a factory `create_cli(service_factory)` instead and document exact registration contract. No copying entire unrelated connector framework.

- [ ] **Step 1: RED tests.** Same profile+visit produces same deterministic ID; other profile differs. DTO/date/text bounds. Private store roundtrip, symlink/permission rejection, cross-profile subject collision and altered profile/token mismatch.

```python
assert event_id(owner, visit) == event_id(owner, visit)
assert event_id(owner, visit) != event_id(other_owner, visit)
service.sync(event)
service.sync(event)
assert fake_calendar.insert_count == 1
```

- [ ] **Step 2: Implement store/OAuth with mocked flow/refresh/userinfo.** Cover exact-scope acceptance/denial, missing auth no-network, verified identity mismatch, refresh persistence. No real auth calls.
- [ ] **Step 3: Implement API/service and failure tests.** create→repeat→question update→cancel; same stable ID after timeout+GET reconciliation; 409 recovery; foreign event no mutation; ETag conflict no overwrite; no attendees; proper escaped description/path, token/body redaction, 401/403/429/5xx/no hidden retries.
- [ ] **Step 4: CLI/docs and verify.** Short TL;DR setup/permission explanation; export unambiguous root integration interfaces. Focused tests, Ruff changed files, mypy src, diff-check, commit owned files. Root does integration/fullsuite/live authorization only at final access gate.

## Official API references checked by root

- [Events insert and caller-specified IDs](https://developers.google.com/workspace/calendar/api/v3/reference/events/insert)
- [Narrow calendar scopes](https://developers.google.com/workspace/calendar/api/auth)
- [Google OpenID user information](https://developers.google.com/identity/openid-connect/openid-connect#obtaininguserprofileinformation)

These docs establish API shape, not live authorization. Google notes sendUpdates=none can affect external calendar syncing; we add no attendees or external-calendar migration and do not promise delivery to third parties.
