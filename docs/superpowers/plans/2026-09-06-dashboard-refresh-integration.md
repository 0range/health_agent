# Finish automatic lab dashboard refresh

TL;DR: a newly confirmed analyte must appear on the already configured dashboard
without asking the owner to run a setup command. Existing-series SQL is live, but
discovered new series currently need a manual bootstrap. Close this integration gap.

## Approved boundary

User requested regular imports, useful evolving lab graphs and no unnecessary
involvement. Reuse existing scheduler and idempotent exact-ownership bootstrap.
No new daemon, UI, provider, schema, automatic approvals or dashboard permission model.

## One bounded task

- Add a dashboard refresh job to existing automation after extraction/Sheets.
- Discover only existing DB profiles with an already saved `labs` dashboard pointer
  for the configured Metabase origin; no unsolicited setup for other profiles.
- Reuse `dashboard setup-labs --profile-id UUID`, no shell interpolation or secrets.
- Missing/unconfigured profiles produce no job. Cross-profile/origin maps do not
  qualify. Stable bounded discovery (at most 1000 profiles), no reads creating files.
- Existing runner timeout/error isolation applies: unavailable Metabase must not
  block ingestion or invalidate Sheets success. GET pages still do no sync.
- Add failing tests for configured-only discovery, other-origin/profile exclusion,
  scheduling after ingestion/extraction/Sheets and independent failure isolation.
- Test synthetic available-series refresh from a newly verified analyte through
  the already existing bootstrap/MockTransport; no production test data.
- Scope: automation models/registry/runner and focused tests plus brief runbook note.
  Do not modify dashboard SQL, credentials, source validation or main README.

Root independently reviews, reruns suite and executes the normal owner sync once.
User acceptance stays at the end. No Calendar OAuth or external browser approvals.
