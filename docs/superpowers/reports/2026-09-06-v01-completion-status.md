# v0.1 completion ledger

TL;DR: integrated v0.1 implementation now passes 1244 tests; final cross-module journeys, live deployment and data acceptance still in progress. User acceptance deferred until the end. Do not redispatch completed work or claim full archive extraction from import counts.

## Current work

1. Recurring reminders, doctor visits/questions/answers, Telegram and medical panel composition complete and reviewed. Compact accessible medical UI4383071 independently reviewed and merged. Empty state now only two collapsed creation forms. Final browser checks ongoing.
2. Separate Calendar adapterdebbf95 and explicit publication workflow41f6edf reviewed/merged. Event edits/prepared questions sync after commit; durable per-profile opt-in; no attendees. Actual Calendar OAuth requires owner at final acceptance, not yet performed.
3. Strict extractionc2d3e96 plus standalone-flag compatibility2781415, source-proven PDF geometry117e9e7 and immutable evidence persistence90fa36a reviewed/merged. Source-linked clinical report/visit-answer QAa3a0d85 and cited clinical locator priorityab734cf reviewed/merged. Source excerpts remain distinct from verified lab measurements.
4. Lab dashboardb846cce reviewed/merged; actual staging Metabase0.63.13 browser confirms same-day measurements not summed, Russian comparison values, and a single common result/reference axis. Unit families remain separate. Profile destination map and panel links repaired15f6e20.
5. Current combined full suite:1244passed/5upstreamSWIGwarnings/19.76s before compact UI merge. Final journey agent implementing test-only five-journey suite; final full suite pending that merge.
6. Production and staging schema0014_v01_workflow_evidence applied. Fresh private backupdata/pre0014-backup/database.dump (0600), container pg_restore list verified. New persistent panel LaunchAgentadc98f9 installed, actual /healthcheck HTTP200; Telegram stopped/reinstalled to load merged code. Actual reboot not tested.
7. Owner archive repair committed after visual verification of all14 source rows across4pages/2documents. Exact source SHA/cells/dates crosschecked against private manifest.584obsolete unmapped parser artifacts rejected via existing audit-preserving review API, not deleted; original documents retained. Repair121PDF/4supportedpages/14inserted/1blocked. Extraction worker maintenance resume still under final audit; no budgets/job counters reset.
8. Live Sheets/dashboard refresh and Yandex report-source answer acceptance running. Archive121docs/289pages is imported, NOT fully normalized. Unsupported KDL/table formats remain unresolved rather than invented data. No second profile credentials or production test fixtures created.

## Deployment checkpoint before this run

Yandex owner-only enabled, Qwen real QA with exact citations accepted; Telegram heartbeat ready and owner ping delivered. PostgreSQL local, Drive/Gmail/WHOOP/Sheets configured. 121 docs,289pages,7dated;584pending,0verified. Do not mistake pending count for extracted clinical coverage. Extraction requests stay budgeted; failed source matching must not be fixed by weakening global evidence validation.

## Decisions

Temporary maintenance: disabled only owner lab extraction worker (budget20 unchanged) after confirming local parser pollution. Source sync/WHOOP/Telegram remain enabled; resume extraction after final pipeline audit. Rejected legacy rows and immutable originals remain recoverable through backup and review audit.

Keep existing stack; no framework migration. Generic clinician question templates may be generated without inventing patient-specific advice. Screening intervals must be explicitly selected by user/clinician. Google Calendar code will use separate authorization and idempotent event IDs, with no attendee invitations. No synthetic test data in production.
