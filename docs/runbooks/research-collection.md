# Room and sleep collection: operations

Study start: **10 September 2026, Europe/Moscow**. The first day is a setup day;
full bedroom-night counting begins after the recorded move from the office.

## Running collection

| Source | Native data | Collection into this database |
| --- | --- | --- |
| Qingping | CO₂, temperature, humidity, PM2.5, PM10, TVOC index, reported noise; verified six-second history | Device collects every 6 s and reports every 60 s; local collector polls every minute. Cloud arrivals can lag several minutes. |
| WHOOP app | HR requested at 6 s, raw stress graph, sleep stage intervals | Independent hourly job, rereading today and up to three previous Moscow days. |
| WHOOP public API | Sleep, recovery, nightly HRV, resting HR, respiratory rate, SpO₂, skin temperature, cycles, workouts and other supported fields | Existing four-hour automation; all available original public API records remain preserved. |

The six-second HR and sensor clocks are independent. Raw times are never rounded
or changed. Stress graph coordinates are not verified UTC timestamps and are not
fabricated into a six-second series. Continuous HRV is not available from the
verified interfaces; the current archive includes nightly HRV. Noise values are
not audio, and brief peaks between readings may be missed. The manufacturer's
archival noise semantics have not been established as Leq/Lmax.

Each participant requires their own profile, public authorization and app-session
configuration; disk planning includes two. See the additional-participant setup below.
Existing historical data before 10 September is retained, but outside this study.
No automatic retention deletion is configured for research records.

## Daily checks

`com.orange.health-agent.research-quality` runs independently every hour and at
10:00 local time (this Mac is configured as Moscow). From 10:00 Moscow onward it
writes/revises the preceding three complete study dates. First scheduled daily
report: **11 September 2026 at 10:00 Moscow**, covering the setup day.

The Qingping collector rereads the previous three full study dates once a day
from 09:00 Moscow, before the daily quality review. It also keeps its hourly
24-hour reconciliation and minute ten-minute overlap. Partial history failures
leave replay eligible for retry, with current ingestion cursor preserved.
Outages longer than the configured lookback or provider retention need a separate
backfill; no collector can recover sensor readings that were never uploaded.

Each private daily JSON contains:

- Actual sample counts, first/last timestamps, occupied six-second bins, coverage
  percentage and all gaps over 12 seconds, including beginning/end gaps.
- Coverage for each air field and room labels. Missing/invalid values are not zero.
- Latest observed exact-day/account HR revision, excluding older UTC-day archives.
- Stress graph point count and an explicit `native_graph_available_unaligned`
  status, distinct from proving continuous stress coverage.
- Presence of a completed main sleep, stage timeline and recovery/HRV fields.

A six-second series passes the collection threshold at >=99% occupied bins and
no gap over 60 seconds. The percentage and gaps remain visible even when accepted.
This is an operational threshold, not the final statistical night-inclusion rule.
Missing nightly sleep/HRV or stress makes a normal day `attention`; the known
partial first day is `setup_day`, never presented as complete.

Current status also checks each collector's success/error state, original sample
freshness, public WHOOP synchronization and available space. HTTP 200 with no new
samples does not pass. A stopped quality checker is marked stale by the status
command after two hours. Checks produce **local reports, not Telegram messages**.

```sh
.venv/bin/health-agent research check
.venv/bin/health-agent research status
.venv/bin/health-agent whoop-detail status
.venv/bin/health-agent qingping status
```

Files (private, ignored by Git):

- `data/research/status.json`: current state and storage estimate.
- `data/research/daily/<profile-id>/YYYY-MM-DD.json`: revisable daily evidence.
- `data/whoop-detail/state.json` and `last_error.json`: source state.
- `.tokens/whoop-web.json`: private owner-authorized app session. Never log it.

Daily reports may first show gaps and improve after a later cloud upload. If the
whole Mac is asleep/offline, neither ingestion nor the local checker can run.
Keep the Mac powered, awake, connected, and Docker running. Its current AC power
configuration disables idle system sleep; battery configuration does not.

## Authentication and raw revisions

App-session renewal uses the observed native WHOOP authentication protocol and
has succeeded without the browser. Renew five minutes before access expiry and
retry once after HTTP 401; preserve the existing refresh token unless rotated.
Identity is checked against the configured public WHOOP connection and the remote
bootstrap response. Transient failures never erase the session and are visible in
quality status. These app interfaces are not the public developer API contract.

Raw WHOOP content versions are addressed by SHA-256 within profile/account,
resource, Moscow calendar and logical date/activity. Identical payloads deduplicate;
`captured_at` is the first observation and `last_seen_at` is the latest observation
of that exact content. This also handles A → B → A source revisions correctly.
Qingping rows deduplicate by device and native timestamp. All histories use their
actual timestamps, without silently manufacturing synchronized observations.

Install/reinstall independent jobs:

```sh
.venv/bin/health-agent whoop-detail install --env-file .env
.venv/bin/health-agent research install --env-file .env
```

## Storage estimate verified on 10 September

At verification: approximately **238 GiB free on the Mac**, **17.8 GiB free in the
Docker database filesystem**, and a roughly **25 MiB database**. The Docker limit
is checked separately; the larger Mac free space does not override it.

31 days at six-second sampling means 446,400 air rows and up to 892,800 native HR
points from two wearers before accounting for archive revisions. Forecasts include
hourly changed WHOOP snapshots for four dates and two profiles, measured maximum
compressed PostgreSQL payload sizes, minimum per-family size assumptions, and a
threefold allowance for row/index overhead and growth. A **10 GiB minimum forecast
plus 5 GiB free reserve** currently fits inside Docker. It is an estimate, not a
preallocated reservation; the checker recalculates it hourly and returns attention
if either filesystem drops below the required reserve. No unrelated Docker volumes
were deleted. Existing backups remain separate; this increment does not establish
an off-machine backup or a new daily database backup schedule.

## Analysis milestones

These are planning estimates, conditional on complete bedroom nights:

- **3–5 nights:** completeness/clock checks, overnight plots, noise events and
  basic room summaries. If the first bedroom night is 10→11 September: 13–15 September.
- **7–10 nights:** exploratory within-person associations and candidate lag windows,
  with explicit missingness and context. No causal conclusion from these patterns.
- **21–28 nights:** reproducible methods, quality and exploratory results report,
  usable as a starting manuscript. Adjacent six-second points are correlated;
  they do not turn a few weeks into thousands of independent nights.

Agree/freeze a small primary question and analysis protocol before examining the
associations. Room move time and notable nights away, illness, alcohol, late food,
exercise and ventilation changes are useful context when known. See the
[research protocol](../research/room-sleep-pilot.md) for interpretation limits.

Sources: [Qingping cloud API](https://developer.qingping.co/cloud-to-cloud/open-apis),
[WHOOP public API](https://developer.whoop.com/api/).

## Additional WHOOP participants

`WHOOP_DETAIL_ACCOUNTS_FILE` optionally points to a private JSON list of additional
participants. The existing `WHOOP_DETAIL_SESSION_FILE` and `WHOOP_DETAIL_ROOT`
remain the first participant. Each additional entry has `profile_id`,
`session_file`, and an independent `root`. Paths are relative to the collector's
working directory. Session files and the manifest must be private (0600).

Create a separate local profile, authorize its public API connection, and verify
its app session against that same WHOOP user before enabling it. The public
four-hour automation already enumerates all registered connections. The hourly
`whoop-detail sync` and `refresh` commands now process every enrolled participant;
one participant's error is recorded separately and does not stop the others.
No second launchd job is needed. Incomplete public authorization remains visible
as a failed detail collection; a manual diagnostic fetch is not proof that the
scheduled pipeline is complete.

The quality job checks each explicitly enrolled profile against the same physical
sensor. The main `status.json` aggregates all participants and reports attention
if any of them is missing or stale. Per-person current evidence lives in
`data/research/participants/<profile-id>/status.json`; daily evidence remains at
`data/research/daily/<profile-id>/YYYY-MM-DD.json`. Another person's fresh pulse
cannot satisfy a missing person's freshness check. Unrelated WHOOP profiles are
not automatically enrolled in the room study.
