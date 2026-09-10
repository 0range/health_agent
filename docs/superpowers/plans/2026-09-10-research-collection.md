# Research Collection Implementation Plan

> Execute inline under the owner's approved collection request; no new permissions or notifications. Preserve the released v0.1.0 tag.

**Goal:** Archive the available WHOOP details and room data starting 10 September 2026, with daily evidence of coverage and enough storage for two WHOOPs for 31 days.

**Architecture:** Independent minute Qingping and hourly WHOOP jobs. A separate hourly local quality job generates a previous-day report after 10:00 Moscow and repeats the last three days to admit late uploads; it also checks current freshness and disk reserve. Source timestamps remain native; coverage is calculated on a separate six-second occupancy grid.

**Tech Stack:** Python, httpx, PostgreSQL JSONB, SQLAlchemy, launchd.

## Constraints

- Study start is 2026-09-10 Europe/Moscow. Existing older historical data is retained outside the study; do not pretend the setup day is complete.
- Only the authenticated owner's WHOOP is configured today. A second profile needs its own authorized session later; capacity estimates include it now.
- HR requests use six seconds; stress graph stays raw and unaligned; HRV is nightly, without invented continuous samples.
- Reports are private local artifacts; no messages to other people, no health data in Git.
- Cloud history cannot guarantee recovery beyond the providers' retention windows. Mac must run on power with Docker available.

## Tasks and acceptance

- [x] Finish `whoop/details.py` and `details_cli.py`: saved session renewal, identity checks, full Moscow day HR/stress and sleep stages. Test renewal failure preserves credentials, rotation/identity, date boundaries, append-only deduplication. Install an independent hourly job and verify real sync.
- [x] Modify `qingping/service.py`: once daily after 09:00 Moscow re-read the previous three complete study days, using paginated history and bounded batch inserts without rewinding the current cursor. Failed backfill must remain eligible for retry. Test a delayed point older than 24 hours and failure recovery.
- [x] Add `research/quality.py` and `research/cli.py`: profile/device-scoped SQL, full-day six-second occupied-bin coverage, edge/internal gaps and required sensor field coverage; latest exact-day WHOOP version, stress availability distinct from aligned data, nightly recovery presence; refresh/token errors, host and database disk reserve. Save daily JSON plus a current status JSON, fail closed on missing sources. Test duplicates, gaps, setup day, timezone boundaries and profile isolation.
- [x] Measure PostgreSQL payload sizes and host/container free space. Estimate 31 days with two WHOOPs including hourly changed snapshots and database overhead; reserve at least 5 GiB beyond estimated archive growth. Do not prune unrelated Docker volumes.
- [x] Run targeted tests, Ruff and mypy. Install and inspect quality schedule. Document first daily report 11 September after 10:00 Moscow, exploratory summaries after 3–5 complete bedroom nights, first associations after 7–10, 21–28-night methods report with explicit uncertainty. Check staged diff for private tokens and push to GitHub.


## Completion evidence

- 192 targeted tests passed (`tests/research tests/qingping tests/whoop tests/automation tests/test_cli.py`); Ruff and mypy passed for touched modules. Existing PyMuPDF/SWIG deprecation warnings remain.
- Native WHOOP session refresh succeeded without another browser login; scheduled detail ingestion returned zero errors. Today's DB archive contained HR points, a nonempty raw stress graph, sleep stage intervals and nightly recovery fields.
- Qingping scheduled sync recovered from the live boundary-overlap defect and resumed native six-second readings; real gaps remain explicit.
- Separate minute sensor, hourly details and hourly/10:00 daily quality LaunchAgents each completed with exit code zero. The quality agent explicitly sets Docker's executable search path; verification was performed through launchd.
- Current quality report returned `ok` for air, detailed/public WHOOP and storage. No completed daily study report exists yet on the setup day; first scheduled report is tomorrow.
- PostgreSQL filesystem had about 17.6–17.8 GiB free at final checks, above the 10-GiB month forecast plus 5-GiB reserve. Host free space was about 237–238 GiB. No unrelated volumes were removed.
