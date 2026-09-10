# Qingping room collection

The sensor sends measurements to Qingping; Health Agent retrieves original
history once a minute. Collection interval and cloud upload interval are separate.
Do not describe an accepted setting as a verified sampling rate until actual
measurement timestamps confirm it.

## Configuration

Register the device in Qingping IoT, then use the same account at
<https://developer.qingping.co/personal/permissionApply>. Put `app_key` and
`app_secret` in `.tokens/qingping-client.json` (permissions 0600, excluded from Git).
No credentials belong in command arguments or committed files.

```sh
health-agent qingping connect --profile-id PROFILE_UUID --room кабинет
health-agent qingping sync
health-agent qingping status
health-agent qingping install --env-file /absolute/path/to/.env
health-agent qingping move --room спальня
# Record a known actual move time, with timezone, if reporting it later:
health-agent qingping move --room спальня --at 2026-09-10T22:00:00+03:00
```

`connect` selects a device automatically only when exactly one is available;
otherwise specify `--mac`. It refuses to overwrite an existing connection.
Collection starts at UTC midnight on the connection day. Initial placement applies
to that first day's imported measurements. Moves affect actual measurement time,
not the time a delayed upload is downloaded. Both configuration and state are
scoped to the selected health profile and device.

Settings paths: `QINGPING_CLIENT_FILE`, `QINGPING_CONNECTION_FILE`, `QINGPING_ROOT`.
The daemon is `com.orange.health-agent.qingping`. It uses its own lock and a
60-second launchd interval, independently of the general four-hour automation.
Logs in `data/qingping/logs/` contain counts/status only. `status` is a local command
showing measurements; avoid pasting it into public reports without review.

## Data and recovery

Original timestamps, vendor fields, units, device identity and room are retained
in PostgreSQL `pilot_records`, domain `shared`, kind `room_measurement`. Source keys
contain `qingping`, MAC and timestamp. Missing/invalid values are omitted from
normalized metrics, never replaced with zero; raw fields remain. TVOC index stays
an index. Noise is the vendor's dB value; no peak/average/weighting semantics are
assumed. Metadata records its own retrieval time separately from measurement time.

Repeated downloads are idempotent. Each minute run overlaps ten minutes; hourly
reconciliation overlaps 24 hours. One-day windows paginate completely before
their cursor advances. Catch-up is bounded to seven new days per run. Data older
than the overlap arriving after its window was processed needs explicit backfill.
No automatic deletion is configured: three/four weeks is a study horizon, not a
retention limit. Existing database backups include these records.

OAuth tokens are cached privately, renewed before expiry, and retried once on
401. Temporary errors preserve credentials and successful checkpoints. Source
freshness is separate from request success; at a one-minute upload interval,
measurements older than five minutes are stale. A missing sensor remains visible
as an error rather than becoming another selected device.

The Mac must be awake, Docker/PostgreSQL running, and cloud/device network access
available. launchd resumes on availability; polling faster cannot reconstruct
measurements the sensor never stored. Validate actual timing and gaps before
using the archive in research.

## Research operation from 10 September 2026

The live device now uses verified 6-second collection / 60-second cloud reporting.
History can include the sample just before the requested starting second; the
collector preserves bounded adjacent readings with their original timestamps,
while rejecting unrelated windows. Batch insertion makes daily replay idempotent
without a transaction per sample. After 09:00 Moscow it also replays the previous
three complete study dates once daily. See [daily quality and storage checks](research-collection.md).
