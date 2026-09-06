# Google Calendar adapter

TL;DR: configure one calendar and one verified Google account per health profile, authorize interactively with the exact `openid`, email, and owned-calendar-event scopes, then let the visits integration call `CalendarService.sync(CalendarEvent(...))`. The adapter has no publish CLI.

Profile configuration and OAuth credentials use separate explicitly supplied private roots. Directories are `0700`, files are atomic `0600`, and symlinks are rejected. A Google subject cannot be shared across health profiles.

Events use a deterministic ID derived from profile and visit UUIDs. Only private summary, bounded preparation questions, dates, timezone, status, and private ownership properties are managed. There are no attendees, notifications, conferencing, attachments, or medical-archive export. Unknown or foreign remote events are never overwritten.

Root integration should register `create_cli(service_factory, profile_validator=database_profile_check)` under its chosen command name. Construct one `CalendarOAuth(client_secrets, profile_store, token_store, GoogleCalendarGateway)` and then `CalendarService(profile_store, token_store, oauth, GoogleCalendarGateway)`. Both gateway factories receive a validated `google.oauth2.credentials.Credentials` object, never the stored JSON envelope. The profile validator must reject missing database profiles before configure/authorize. Call `sync` only after the visit transaction closes.
