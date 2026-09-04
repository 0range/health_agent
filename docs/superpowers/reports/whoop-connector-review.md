# WHOOP connector review

Review target: `codex/v1-whoop` at `424b3e3`, relative to `ed29e5f`.

## Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

The connector has a sound foundation, but it is not safe to ship for live OAuth and
unattended sync until findings 1–5 are fixed. Findings are ordered by severity.

## Findings

### 1. High — WHOOP downgrade silently deletes the complete source archive

`alembic/versions/0005_whoop.py:393-410` drops every WHOOP view and table,
including `whoop_raw_records`, normalized history, connections, and sync audit
runs, without checking whether they contain data. The round-trip test at
`tests/whoop/test_schema.py:145-153` exercises only the disposable database and
therefore makes this destructive behavior look like a successful migration gate.

This is a material data-loss path for a personal health archive and is inconsistent
with the fail-closed migration approach already established in the foundation
migrations. The downgrade should refuse while any WHOOP data/provenance exists (or
require an explicit, separately documented export/destructive operation), and the
test should prove both empty success and populated refusal with data preserved.

### 2. High — reauthorization can replace the usable token before profile/account registration succeeds

`complete_whoop_authorization()` writes the newly issued bundle at
`src/health_agent/whoop/auth_service.py:46-72`; only afterwards does the CLI open a
database transaction and call `register_authorized_connection()` at
`src/health_agent/cli.py:168-184`. If the profile does not exist, the account name is
already bound to another WHOOP identity, or the database is unavailable, the DB
operation fails after the old token file has been overwritten. The database then
still describes identity A while the token path contains identity B. A subsequent
sync fails the identity check, and the prior refresh token may no longer be
recoverable.

Validate the target profile/account and expected identity before publishing the
token, then stage and atomically coordinate token publication with DB registration
(with compensation if the DB commit fails). Validate the returned scopes before
marking `auth_status=connected` as well; currently any returned scope tuple is
accepted even if one of the required read scopes is absent. Account-key validation
should also happen before opening the browser and exchanging a code.

### 3. High — refresh-token rotation is not concurrency-safe

`WhoopClient` creates a lock per client instance
(`src/health_agent/whoop/client.py:41-61`), and `_refresh_token()` reloads and always
refreshes the current bundle without checking whether a preceding waiter already
rotated it (`:122-134`). Two requests on one instance can therefore refresh in
sequence unnecessarily; two CLI/scheduler processes are not coordinated at all.
One process can invalidate the access/refresh bundle another process is using and
then atomically overwrite its newer file.

This is not theoretical protocol conservatism: the official WHOOP OAuth guide says
refresh invalidates the existing access token, rotates the refresh token, and
explicitly warns about simultaneous refresh requests. Use one per-token
cross-process lock plus compare/reload semantics (or a single serialized refresh
worker), and add a concurrency test.

Official reference: <https://developer.whoop.com/docs/developing/oauth/>

### 4. High — an older revision can replace the newest normalized current row

`store_normalized_record()` updates a current row whenever its raw ID differs
(`src/health_agent/whoop/repository.py:158-174`). It never compares the incoming
`source_updated_at` with the timestamp already stored on cycle/recovery/sleep/workout.
Consequently, an out-of-order or eventually consistent response appends a valid raw
revision but can move the current row backwards to older values. A later identical
fetch of the genuinely newest payload may repair it, but until then dashboards and
agent queries use stale data; if that record falls outside the seven-day overlap it
may remain stale indefinitely.

Keep every raw revision, but only advance current when the source update timestamp
is newer (with an explicit deterministic rule for equal/null timestamps). Add an
out-of-order revision test. This directly violates the plan requirement to point
current rows at the latest raw revision.

### 5. High — official fractional resting heart rate is silently truncated

WHOOP defines recovery `resting_heart_rate` as a floating-point `number`, but
`_normalize_recovery()` sends it through `_integer()`
(`src/health_agent/whoop/normalize.py:118-121`) and both ORM/migration store it as
an integer. `int(64.9)` silently becomes `64`; the current synthetic test uses an
integer and cannot detect this. Use lossless numeric storage and test a fractional
official response.

Official schema: <https://api.prod.whoop.com/developer/doc/openapi.json>

### 6. Medium — provenance is profile-safe but does not prove resource/record identity

The composite raw foreign key proves only `(raw_record_id, profile_id,
connection_id)`. It does not prove that a `whoop_cycles(external_id=...)` row points
to a raw record with the same external ID and `resource_kind='cycle'`; equivalent
cross-kind/cross-ID links are possible inside one connection. The sync also stores
collection payloads without checking their `user_id` against the verified
connection identity. The report's phrase “strict composite profile+connection
provenance” is true only at that limited boundary, not for the claimed source
revision itself.

Enforce raw kind/external-ID lineage at the database boundary and reject a payload
whose official `user_id` differs from the connected WHOOP user. Extend schema tests
beyond the existing cross-profile case.

### 7. Medium — normalized storage omits official source-only fields promised by the plan

Task 3 explicitly requires typed dashboard metrics plus official response fields
without typed columns. The normalized models contain neither an extras/source JSON
column nor fields such as official `created_at` (and the still-documented legacy
activity IDs). The immutable raw payload avoids irreversible loss, but callers must
reparse raw JSON and the implemented schema does not meet the stated normalized
contract. Add a source/extras field or explicitly type the remaining official
fields; keep raw as the authoritative copy.

### 8. Medium — `429` handling does not always respect the official reset time

`_retry_delay()` clamps `X-RateLimit-Reset` to 60 seconds
(`src/health_agent/whoop/client.py:177-186`). WHOOP documents the value as the
seconds until the limiting window resets and has both minute and daily limits. If
the active reset is greater than 60 seconds, the client retries early up to four
times and fails. Because a failed sync rolls back every fetched page and leaves no
per-resource checkpoint, a sufficiently rate-limited full backfill restarts from
the beginning on the next invocation.

Honor the reset value or stop cleanly with a deferred/retry-at status rather than
retrying before reset. Also reserve one retry of the original request after a 401:
with `max_attempts=1`, or a 401 on the final attempt after transient failures, the
code rotates the token but never retries that request as promised by Task 2.

Official reference: <https://developer.whoop.com/docs/developing/rate-limiting/>

### 9. Medium — operator status and views can conceal important data state

`whoop status` derives `auth=connected` only from the database
(`src/health_agent/whoop/status.py:34-76`); it does not check whether the selected
profile/account token file exists or is readable. Deleting/corrupting that file
therefore leaves a reassuring connected status until sync fails. The status should
report token readiness separately without exposing secrets.

The views do preserve `profile_id` and use profile+connection join conditions, but
`whoop_daily_health` omits cycle/recovery/sleep score states and calibration status,
making absent, pending, unscorable, and calibrating values indistinguishable. The
source-status view omits recovery count, and no Metabase-ready view exposes the
current weight together with its `observed_at`. These do not mix profiles, but they
should be addressed before calling the views a complete dashboard contract.

### 10. Low — execution documentation is internally inconsistent

The separate SDD report records 98 passing tests, lint/type gates, and a migration
round trip, while every implementation checkbox in
`docs/superpowers/plans/2026-09-04-health-agent-v1-whoop.md` remains unchecked. The
plan promises `--profile <slug>` but the delivered CLI uses `--profile-id <UUID>`.
The runbook says the commands work for a second profile but provides no command or
reference for creating that profile. Reconcile these documents so the operator can
distinguish completed mocked gates from live acceptance and can actually perform
the multi-profile flow.

## Confirmed behavior

- As of 2026-09-04, the authorization/token URLs, API base, six v2 resource paths,
  and requested scopes match WHOOP's official OAuth guide and OpenAPI document.
- The eight-character state, callback state comparison, exact loopback callback
  path, and local-only callback binding are implemented. The default callback still
  requires the documented live Developer Dashboard/browser acceptance.
- Token paths are sanitized by profile/account; successful writes use `0700`
  directories, a `0600` temporary file, `fsync`, atomic replace, and a final `0600`
  mode. `.tokens/` and `.env` are ignored by Git. Plain local files are appropriate
  for the explicitly non-enterprise single-Mac boundary.
- Pagination correctly reads `next_token`, sends `nextToken`, retains UTC bounds,
  rejects loops, and uses the official maximum collection page size of 25.
- Full sync omits `start`; incremental sync uses the accepted seven-day overlap from
  last successful sync. A resource failure rolls back raw/normalized page data and
  does not advance freshness while retaining a safe failed-run record.
- Raw revisions are canonically idempotent within profile+connection+resource+ID;
  changed payloads append, while normalized rows remain one-per-origin. Profile and
  connection keys prevent cross-profile joins/upserts.
- Profile, body, cycle, recovery, sleep, and workout are all fetched. Unscored rows
  survive with nullable metrics. Weight is correctly modeled as a current snapshot:
  `observed_at` moves on each successful fetch and no normalized weight history is
  fabricated.
- All four declared views include `profile_id`, and joins include both profile and
  connection boundaries. No WHOOP dashboard/card is delivered on this branch, so
  future cards must still apply the design's mandatory explicit profile filter.
- README and the SDD report truthfully say tests are mocked and no live account/data
  was used. They do not claim that unattended scheduling or a real dashboard is
  already installed.

## Live-only assumptions and unverified handoff

- WHOOP Developer Dashboard acceptance of the exact loopback redirect URI and one
  complete browser callback/token exchange have not been tested.
- Real response variation, old-account history volume, pagination stability,
  eventual consistency, missing/partial scopes, and the behavior at real minute or
  daily limits have not been tested.
- No launchd/cron service schedules the promised several-times-daily sync in this
  branch; the delivered interface is manual CLI plus reusable functions.
- No live Metabase card/dashboard consumes these views yet. The database views are
  query inputs, not proof of the full WHOOP dashboard scenario.

## Review method

Read the approved v1 design, WHOOP implementation plan, runbook, branch diff/current
files, and the SDD report at
`.superpowers/sdd/2026-09-04-health-agent-v1-whoop/whoop-report.md` in the main
worktree. Official behavior was checked against WHOOP's OAuth, pagination,
rate-limit, API, and current OpenAPI pages. Per instruction, this review did not
modify implementation and did not rerun tests; gate results above are reported
evidence, not independently reproduced by this review.
