# Qingping collection

The user selected the official cloud API setup, connected the monitor to Qingping
IoT, and signed into the developer portal. Initial read-only token, device-list,
and history requests succeeded. This implements the previously discussed flow:
monitor → Qingping cloud → scheduled Health Agent collection → local PostgreSQL.

## Scope

One explicitly selected device and health profile per local installation. Preserve
the vendor product information and raw measurement fields. Normalize temperature,
humidity, CO₂, particulate matter, noise, battery and TVOC index separately; an
index is not a VOC concentration. Device MAC and credentials remain outside Git.

Use the existing profile-scoped `PilotStore`, domain `shared`, kind
`room_measurement`; deduplication key includes provider, device and measurement
timestamp. Existing coaches query their own kinds, so collection does not replace
diary entries or flood their context. Analysis/charts and automatic room advice
are subsequent work, not part of this connector.

Record explicit room placements with effective timestamps in private connection
configuration. Initial collection is in the office (`кабинет`). A move affects
measurements from its effective time, including data received late. Do not infer
that a device was in the bedroom from sleep times. Moving can be dated explicitly
and must update the room assigned to already imported affected records.

## Reliability

Read official history with pagination, fixed one-day windows and a 10-minute overlap on minute runs, plus a 24-hour overlap once an hour
to recover delayed uploads. Only advance the cursor after a complete window is
stored. Bound catch-up to seven new days per execution and continue next time.
Serialize sync/move/configuration using the existing filesystem lock. Replayed
requests must not create duplicate measurements.

Cache the client-credentials token privately across runs; renew before expiry or once on HTTP
401. Temporary HTTP failures never erase credentials. Fixed error codes only in
logs. Credentials/configuration/state are private files excluded from Git.

The existing general connector job runs every four hours. Give Qingping its own
one-minute launchd job, reusing the existing LaunchdManager with sensor-specific
paths and label. Preserve existing jobs. Show last successful sync, newest device
timestamp, measured values and stale status (at least five minutes or three device
report intervals). Mac/Docker/cloud availability remain operational dependencies.

## Acceptance

Tests cover auth renewal, pagination, temporary failures, deduplication, cursor
recovery, location changes, missing values, profile isolation and launcher paths.
Verify real history is persisted, replay is idempotent, and the installed launchd
job completes successfully. No new Telegram notifications are part of this work.

Sources: [OAuth](https://developer.qingping.co/cloud-to-cloud/oauth),
[API](https://developer.qingping.co/cloud-to-cloud/open-apis).

## User refinement: 10 September

Collect at the highest supported practical resolution for a 3–4 week exploratory
study. Device collect/report intervals were changed from 900 to 60 seconds and
confirmed by API read-back. Preserve minute samples continuously, with no 28-day
auto-deletion. WHOOP detailed time-series access is investigated separately.

The user subsequently selected matching approximately six-second native cadence
for Qingping and WHOOP HR. Qingping settings now request collection every six
seconds and upload every 60 seconds; actual six-second timestamp spacing was
verified. The collector keeps every original sample; its download schedule remains
one minute. Earlier 15- and five-second settings were verified during setup only.
