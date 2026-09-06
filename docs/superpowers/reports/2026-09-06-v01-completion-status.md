# v0.1 completion ledger

TL;DR: autonomous development in progress; user acceptance deferred until the end by explicit user instruction. Do not redispatch completed Yandex work or claim v0.1 complete from test count.

## Current work

1. Recurring reminders — implementing from plan 2026-09-06-recurring-reminders.
2. Local doctor visits/questions/answers — implementing from plan 2026-09-06-doctor-visits.
3. Medical extraction — local audit found all 584 inherited pending rows unmapped, 571 with unsupported units; no mechanically safe bulk approval subset. Investigating initial importer and PDF geometry before changing evidence validation.
4. Calendar adapter, shared UI/Telegram integration, lab history/Sheets/dashboard, five-scenario automated acceptance — remaining; no new owner interaction until needed for an actual access decision.

## Deployment checkpoint before this run

Yandex owner-only enabled, Qwen real QA with exact citations accepted; Telegram heartbeat ready and owner ping delivered. PostgreSQL local, Drive/Gmail/WHOOP/Sheets configured. 121 docs,289pages,7dated;584pending,0verified. Do not mistake pending count for extracted clinical coverage. Extraction requests stay budgeted; failed source matching must not be fixed by weakening global evidence validation.

## Decisions

Keep existing stack; no framework migration. Generic clinician question templates may be generated without inventing patient-specific advice. Screening intervals must be explicitly selected by user/clinician. Google Calendar code will use separate authorization and idempotent event IDs, with no attendee invitations. No synthetic test data in production.
