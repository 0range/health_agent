# Google Calendar adapter

TL;DR: configure one calendar and one verified Google account per health profile, authorize interactively with the exact `openid`, email, and owned-calendar-event scopes, then let the visits integration call `CalendarService.sync(CalendarEvent(...))`. The adapter has no publish CLI.

Profile configuration and OAuth credentials use separate explicitly supplied private roots. Directories are `0700`, files are atomic `0600`, and symlinks are rejected. A Google subject cannot be shared across health profiles.

Events use a deterministic ID derived from profile and visit UUIDs. Only private summary, bounded preparation questions, dates, timezone, status, and private ownership properties are managed. There are no attendees, notifications, conferencing, attachments, or medical-archive export. Unknown or foreign remote events are never overwritten.

Root integration should register `create_cli(service_factory, profile_validator=database_profile_check)` under its chosen command name. Construct one `CalendarOAuth(client_secrets, profile_store, token_store, GoogleCalendarGateway)` and then `CalendarService(profile_store, token_store, oauth, GoogleCalendarGateway)`. Both gateway factories receive a validated `google.oauth2.credentials.Credentials` object, never the stored JSON envelope. The profile validator must reject missing database profiles before configure/authorize. Call `sync` only after the visit transaction closes.
# End-to-end visit publication

Visits stay local until you explicitly choose **Опубликовать в Calendar** on the
profile medical page, send `/visit_calendar CODE` in your bound Telegram chat,
or run `health-agent visit calendar CODE --profile-id UUID`.

Configure and authorize the existing database profile first:

```sh
health-agent calendar configure --profile-id UUID
health-agent calendar authorize --profile-id UUID --interactive
health-agent calendar status --profile-id UUID
health-agent calendar sync --profile-id UUID --limit 100
```

These are operator instructions, not actions performed by installation or tests.
No invitations or notifications are sent. Authorization needs explicit interactive
selection; automatic retries never open a browser. Missing authorization keeps the
publication **queued**, not published. A successful remote response confirms the
publication; status pages do not perform a remote check.

`GOOGLE_CALENDAR_ROOT` defaults to `data/google-calendar`, containing separate
`profiles/`, `tokens/` and `locks/` directories. `GOOGLE_CALENDAR_CLIENT_SECRETS`
defaults to `data/secrets/google-oauth-client.json`; Calendar token files are never
shared with Drive, Gmail or Sheets. CLI validates the profile in PostgreSQL before
writing configuration or credentials. GET/status reads create no directories.

An explicit opt-in creates one `visit_calendar_publications` record, constrained
by the visit/profile composite foreign key. Subsequent Telegram, panel and CLI
note/status/time edits commit locally before an opted-in sync attempt. Questions
only are sent: first 20, up to 1000 characters each, with visible truncation notes;
answer notes and the medical archive are never included. The exact sent event is
fingerprinted. Edits made during a request stay queued for the next attempt.

The existing automation runner discovers opted-in profiles (up to 1000) and runs
at most 100 visits per profile, rotating by oldest attempt. Unchanged successful
snapshots require no external request. Failure retains local edits and queues the
attempt. Per-visit private file locks serialize duplicate deliveries. The profile
configuration lock prevents retargeting a request in flight. Once bound, a changed
Google subject or calendar ID is rejected with `calendar_target_mismatch`; changing
configuration does not silently migrate already-published visits.

The medical page shows per-visit local/queued/confirmed state, local Calendar
connection state and backlog. Healthcheck includes the same safe connection card.
Returned event links must be HTTPS Google Calendar URLs; other links are discarded.
CSRF, origin and existing request-size checks protect the publication POST.

Staging explicitly configures isolated Calendar, Drive, Sheets, automation and AI
credential paths, in addition to prior connector paths. Missing required staging
paths fail closed, and inherited inline OpenAI/Yandex secrets are removed. Existing
`.env.staging` overrides must add the new keys from `.env.staging.example`.
