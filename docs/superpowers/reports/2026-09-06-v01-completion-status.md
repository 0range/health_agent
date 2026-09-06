# v0.1 completion ledger

TL;DR: autonomous development in progress; user acceptance deferred until the end by explicit user instruction. Do not redispatch completed Yandex work or claim v0.1 complete from test count.

## Current work

1. Recurring reminders — reviewed and merged (`9eac0c7`); production migration 0009 applied after private pg_dump backup. Combined suite: 1026 passed, three stale schema-head assertions need integration update (not a green full suite yet).
2. Local doctor visits/questions/answers — reviewed and merged `ec7954c` via6c8fb6b. UI/Telegram integration353adcc fixes passed291focusedtests and root final review; merged and production schema0011 applied. Telegram reinstalled to load handlers. Panel process still needs restart. Combined1092passed/1failed: existing exact five-field lab row with standaloneflag needs narrow compatibility restoration, not altered fixture expectation.
3. Medical extraction — strict shared parser throughc2d3e96 reviewed/merged after two authenticity-fix rounds (173focusedtests). Read-only archive dryrun289pages gives0strictflatrows because PDF extraction is column-major. Pure source-proven PDF geometry adapter implementing; root must add immutable alternate evidence persistence/importer/archive repair. All584oldpending remain untouched, no bulkapproval. Extraction worker still temporarily disabled.
4. Calendar adapter1aefe95 review found broken concrete transport/OAuth and missing failure tests; fix round1 running. Separate lab history dashboard implementing. Five-scenario automated acceptance, source-linked document/visit answers in QA, Calendar composition, final deployment and user acceptance remain. No new owner interaction until actual access decision.

## Deployment checkpoint before this run

Yandex owner-only enabled, Qwen real QA with exact citations accepted; Telegram heartbeat ready and owner ping delivered. PostgreSQL local, Drive/Gmail/WHOOP/Sheets configured. 121 docs,289pages,7dated;584pending,0verified. Do not mistake pending count for extracted clinical coverage. Extraction requests stay budgeted; failed source matching must not be fixed by weakening global evidence validation.

## Decisions

Temporary maintenance: disabled only owner lab extraction worker (budget20 unchanged) after confirming local parser pollution. Source sync/WHOOP/Telegram remain enabled; resume extraction after strict parser and archive reconciliation acceptance.

Keep existing stack; no framework migration. Generic clinician question templates may be generated without inventing patient-specific advice. Screening intervals must be explicitly selected by user/clinician. Google Calendar code will use separate authorization and idempotent event IDs, with no attendee invitations. No synthetic test data in production.
