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

---

## Fix-round re-review at `9ef3bbd`

### Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

The fix closes the original data-model, downgrade, ordering, metrics, retry, view,
and documentation findings. It also correctly serializes refresh-token rotation
when considered on its own. One new cross-resource deadlock remains a live blocker,
and the token/DB fail-safe path still has smaller consistency and status gaps.

### Remaining findings

#### 1. High — `auth` and `sync` can deadlock because they acquire file and database locks in opposite orders

Authorization first enters `TokenStore.replacement()`, which holds the account's
exclusive `flock`, and only then opens/updates the database connection
(`src/health_agent/whoop/auth_service.py:106-119`). Synchronization does the
opposite: it first locks the connection row with `SELECT ... FOR UPDATE`
(`src/health_agent/whoop/sync.py:71`) and then calls the client, whose first token
load takes the same account's shared file lock
(`src/health_agent/whoop/client.py:112-118`,
`src/health_agent/whoop/tokens.py:224-226`).

For example, a scheduled sync can lock the connection, a concurrent reauthorization
can acquire the file lock, sync can then wait for that file lock, and authorization
can wait in `register_authorized_connection().flush()` for sync's connection-row
lock. PostgreSQL cannot detect the other half of this cycle because it is an OS
file lock. This is especially plausible when reauthorizing a connection whose
`auth_status`/`last_error_code` must be updated. Establish one global lock order for
both flows (or one per-account orchestration lock acquired before either resource)
and test concurrent `auth` versus `sync`, not only refresh versus refresh. Until
then unattended sync and human reauthorization must not be allowed to overlap.

#### 2. Medium — token publication is exception-compensated, but not crash-safe and has a post-replace exception hole

The ordinary database-commit failure tested in
`tests/whoop/test_auth_service.py:122-166` is now compensated correctly. However,
the candidate file is published before the surrounding session context commits
(`src/health_agent/whoop/auth_service.py:106-119`), so process termination after
the file replace and before DB commit leaves a new token with an old or absent
database registration. There is no durable pending marker or startup reconciliation
for that state.

There is also an ordinary exception window inside `_save_unlocked()`: `os.replace`
installs the candidate at `src/health_agent/whoop/tokens.py:118`, followed by chmod
and directory fsync through line 125, but `TokenReplacement.publish()` sets
`_published=True` only after `_save_unlocked()` returns (`:247-252`). If a
post-replace chmod/open/fsync fails, the replacement context calls `rollback()`,
sees `_published=False`, and leaves the candidate exposed. Mark the uncertain
publication before fallible post-replace work and make recovery idempotent; add a
durable reconciliation strategy if the plan continues to call token+DB publication
atomic. The plan and implementation report currently overstate this guarantee.

#### 3. Medium — unreadable tokens and invalid upstream identities bypass the safe sync audit

`WhoopClient._load_token()` lets an initial `TokenStoreError` escape directly
(`src/health_agent/whoop/client.py:112-118`), while `sync_whoop()` catches
`WhoopAuthorizationRequired`, API, normalization, repository, and SQLAlchemy errors
but not `TokenStoreError` (`src/health_agent/whoop/sync.py:146-158`). A corrupt or
permission-broken token therefore crashes the CLI and rolls back the attempt/run
instead of recording `reauth_required`, even though `whoop status` can already
classify the same file as `unreadable`.

Malformed identity values have a similar hole: profile `user_id` and recovery
`cycle_id` are accepted as string IDs and then converted with bare `int(...)`
inside the normalizers (`src/health_agent/whoop/normalize.py:61-69,115-123`). The
resulting plain `ValueError` is outside the sync error set. Convert token-store
failures to authorization-required, make every upstream conversion raise
`WhoopNormalizationError`, and prove both cases persist a safe failed run without a
traceback or secret-bearing exception chain.

#### 4. Medium — refresh tokens and `status` do not enforce the required scope set

Initial authorization and publication now reject missing scopes, closing the main
part of original finding 2. On refresh, however, `WhoopOAuth.refresh()` parses the
returned scopes and `TokenStore.rotate()` saves the token without checking
`WHOOP_SCOPES` (`src/health_agent/whoop/oauth.py:88-97,99-133`;
`src/health_agent/whoop/tokens.py:161-177`). `whoop status` reports any readable,
unexpired token as `ready` based only on expiry
(`src/health_agent/whoop/status.py:44-50`). If WHOOP returns a reduced or malformed
scope set, the database still says connected with the original scopes, status says
ready, and a later resource request becomes generic `sync_failed` rather than
`reauth_required`. WHOOP's current refresh example returns `offline` plus the other
requested scopes, but this is still a live boundary that should fail closed and be
tested.

#### 5. Low — the amended migration revision has no upgrade path from the reviewed pre-fix `0005`

The final schema is correct when upgraded freshly from the stated base
`305b837`/`0004_chart_integrity`, and populated downgrade now fails closed. The fix
edits the already-created `0005_whoop` revision in place, though. Any local or CI
database that applied the pre-fix `0005` remains stamped at head, so `alembic
upgrade head` will not add `resource_kind`, `external_id`, `source_values`, the
stronger foreign keys, numeric RHR, or updated views. This is acceptable only under
the explicitly verified assumption that the pre-fix revision was never retained
outside disposable review databases. Otherwise add a successor migration (or
clearly require rebuilding a confirmed data-free pre-release database).

### Original finding disposition

- **1, downgrade data loss:** resolved for a fresh final schema; every WHOOP table
  is checked and populated downgrade refuses without deleting data.
- **2, reauthorization:** profile/account validation, remote identity, initial
  scopes, flush/commit exception compensation, and old-token restoration are
  implemented. Crash/post-replace consistency remains in finding 2 above.
- **3, refresh concurrency:** shared/exclusive cross-process file locking plus
  compare/reload prevents duplicate rotation. Its interaction with DB locking
  introduces finding 1 above.
- **4, revision ordering:** resolved with deterministic
  `(updated_at presence, updated_at, payload hash)` ranking; losing raw revisions
  stay archived and do not replace current rows.
- **5, fractional RHR:** resolved with `Numeric`/`Decimal` and a fractional test.
- **6, provenance and remote identity:** resolved at both database lineage and
  collection-payload identity boundaries. Body has no official payload `user_id`
  and is safely fetched only after verifying the profile under the same token.
- **7, source-only fields:** resolved by retaining the complete source object in
  every normalized row as well as the immutable raw revision.
- **8, reset/401:** resolved as specified: reset seconds are no longer capped, and
  one 401 refresh/retry is independent of the transient-attempt budget. A real
  daily-limit response can still intentionally keep a transaction/connection lock
  open for many hours; operational acceptance should decide whether deferred exit
  is preferable before scheduling.
- **9, status/views:** resolved for the requested fields and profile boundaries:
  token file state, recovery count, score/calibration states, and dated body/weight
  snapshot are exposed. Required-scope validation remains finding 4 above.
- **10, docs:** plan checkboxes, CLI option, second-profile command, mocked/live
  boundary, and view list are reconciled. Only the atomic-publication wording noted
  above remains too strong.

### Live-only concerns still requiring acceptance

- The exact loopback redirect remains unverified in a real WHOOP Developer app and
  browser, as disclosed.
- No real account has exercised full historical volume, live pagination, rotated
  scopes/tokens, daily rate limits, or source payload variation.
- Literal `X-RateLimit-Reset` handling can sleep for the daily window while holding
  the per-account database row lock and an open transaction. This preserves
  correctness but may be operationally undesirable on the scheduled Mac service.
- No scheduler or live Metabase dashboard/card is part of this connector branch;
  the views are profile-aware query inputs only.

Per instruction, this fix-round review inspected the diff and existing reported
gates but did not modify implementation or rerun tests.

---

## Hardening round 2 re-review at `3ca3270`

Review delta: `68c9400..3ca3270`.

### Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

Round 2 resolves the cross-resource deadlock, unsafe audit/status, reduced-scope,
long-rate-limit, credentials-file, and documented pre-release-lineage findings.
The durable journal also handles process termination on either side of the
database commit and the previously identified post-replace fsync failure. One
ordinary post-commit exception window still breaks the token/database atomicity
claim, so the connector should not yet perform the live authorization handoff.

### Remaining finding

#### 1. Medium — an exception after DB commit but before journal commit restores the wrong token and destroys recovery evidence

`publish_whoop_authorization()` publishes the candidate inside the database
session context and calls `replacement.commit()` only after that context exits
(`src/health_agent/whoop/auth_service.py:121-135`). The database session context
commits as part of its exit. If the commit succeeds but a later part of context
exit raises (for example `Session.close()`), or a `KeyboardInterrupt`/other
`BaseException` arrives after context exit and before line 135, control unwinds
the replacement with its `_committed` flag still false.

`TokenStore.replacement()` then unconditionally calls `rollback()`
(`src/health_agent/whoop/tokens.py:322-326`), and `rollback()` restores the prior
token and clears the journal without consulting the now-committed database
generation (`:454-466`). The database therefore retains the candidate generation
while the filesystem contains the old token and no journal. Later
`recover(..., committed_generation)` has no evidence from which to repair the
split state. This can strand an account or restore a token that WHOOP has already
made unusable.

Keep an unresolved coordinated journal on exceptional replacement exit, then
reconcile it against `WhoopConnection.token_generation` after the token lock is
released but while the outer operation lock is still held. Equivalently, make the
replacement exit path consult a committed-generation callback before choosing
candidate versus previous. Add a regression session context that commits the new
generation and then raises during post-commit cleanup; recovery must retain the
candidate. Preserve the existing commit-failure test, which must still restore the
previous token.

### Prior round-1 finding disposition

- **High — auth/sync lock inversion: ADDRESSED.** Auth publication, sync, and
  status now acquire the same exclusive per-profile/account operation flock
  before any token-file or PostgreSQL lock. Network sync work and reauthorization
  for one account serialize outside both inner resources. The PostgreSQL-backed
  threaded regression deliberately overlaps a sync holding the operation lock
  with publication and proves both complete, with the new token winning. Direct
  refresh rotation retains its per-token cross-process serialization.
- **Medium — crash journal and post-replace failure: PARTIALLY ADDRESSED.** A
  mode-0600 journal durably records previous/candidate bundles and a UUID
  generation before replacement. `whoop_connections.token_generation` commits in
  the registration transaction; startup recovery selects old versus candidate by
  that committed value. Candidate publication is marked before fallible work,
  uses a pre-replace mode setting, and injected directory-fsync failure restores
  the old token. Tests cover interrupted uncommitted/committed journals and
  standalone refresh journals. The post-DB-commit exception path above remains.
- **Medium — unreadable tokens and malformed identities bypass audit: ADDRESSED.**
  Operation/recovery/load token-store failures become
  `WhoopAuthorizationRequired`; sync persists `reauth_required`, while status
  safely reports `unreadable`. Required numeric identities now use helpers that
  consistently raise `WhoopNormalizationError`, and the added PostgreSQL-backed
  tests verify safe failed runs without secret-bearing output.
- **Medium — required refresh/status scopes: ADDRESSED.** OAuth refresh and the
  client both require every `WHOOP_SCOPES` entry before saving/using a rotated
  token. Existing token use and status readiness apply the same complete-set
  check; reduced grants remain unsaved and are classified as reauthorization.
- **Low — amended `0005` migration lineage: ACCEPTED UNDER THE DOCUMENTED
  PRE-RELEASE ASSUMPTION.** The revision remains a direct
  `0004_chart_integrity -> 0005_whoop` migration and now includes
  `token_generation` and deferred retry fields in both Alembic and ORM metadata.
  The report/runbook explicitly state that the former reviewed `0005` was never
  deployed with real data and that any retained review database must be confirmed
  data-free and rebuilt. The reported green suite includes empty upgrade,
  downgrade/upgrade, populated-downgrade refusal, schema fingerprint, and Alembic
  metadata checks.

### Other round-2 checks

- Long `429` reset windows now raise a typed deferred result without sleeping.
  The nested data transaction rolls back, while the outer audit records
  `status=deferred`, safe `rate_limited`, and the exact `retry_at`; the CLI treats
  deferred as a successful scheduled outcome. Only bounded inline waits can occur
  while the connection row is locked.
- Credential loading defaults to the Git-ignored
  `.tokens/whoop-client.json`, rejects symlinks/non-regular files and modes other
  than `0600`, wraps the secret in `SecretStr`, and emits value-free errors. A
  complete environment ID/secret pair overrides the file, while a partial pair
  fails closed. Token root, credential path, redirect URI, and database URL remain
  configurable for isolated staging; `.env.example` contains no credential.
- Normal CLI status/sync output is allowlisted to state, timestamps, counts, mode,
  and safe error codes. Token values, upstream bodies, profile identity fields,
  and exception contents are not printed. The journal/token files remain under
  validated per-profile/account paths with private modes and atomic writes.
- No regression was found in profile/connection isolation, raw lineage,
  deterministic newest-revision selection, numeric resting HR, protected
  downgrade, profile-aware views, pagination, or the independent 401 retry.

### Live-only concerns after the implementation finding is fixed

- The configured WHOOP Developer application, exact loopback redirect, browser
  callback, authorization-code exchange, and first full sync still require the
  disclosed human live acceptance run. No real token or account payload has been
  used in the reported gates.
- Real historical volume, response variation/eventual consistency, pagination,
  refresh-token rotation, reduced live scopes, and minute/daily rate-limit headers
  remain unexercised. The bounded-inline/deferred policy is locally covered but
  still needs observation against real WHOOP responses.
- The pre-release migration conclusion depends on the documented operational fact
  that no real database retained the earlier `0005_whoop`. Any such database must
  be inspected for WHOOP data and handled according to the rebuild warning rather
  than upgraded in place.
- The tests provide strong injected-failure and local-filesystem evidence, but
  actual power-loss durability ultimately depends on the target macOS filesystem's
  `fsync`/atomic-rename behavior. No scheduler or live Metabase card is included in
  this branch.

### Review method

Read the round-1 review/report, round-2 plan, full `68c9400..3ca3270` delta, current
token/auth/client/sync/status/configuration/migration code, and the added tests.
The reported final gates are 143 passing tests, clean Ruff/mypy/diff checks, and
the migration checks listed above. Per instruction, none were rerun during this
review. Only this review report was changed.

---

## Hardening round 3 final re-review at `f92077b`

Review delta: `e9b219f..f92077b`.

### Verdict

- **SPEC: PASS**
- **QUALITY: APPROVED**
- **OVERALL: APPROVED FOR LIVE ACCEPTANCE**

No implementation finding remains from the round-2 review. The final fix preserves
the database/token atomicity invariant across both ordinary exceptions and
`BaseException` interruption after candidate publication. This branch is ready for
the disclosed live OAuth and full-sync acceptance work.

### Round-2 finding disposition

- **Medium — post-DB-commit exception restored the old token and destroyed recovery
  evidence: ADDRESSED.** Exceptional exit from `TokenStore.replacement()` no longer
  guesses that the database rolled back. It leaves the coordinated, mode-0600
  journal intact. `publish_whoop_authorization()` then releases the inner token
  lock, retains the outer account-operation lock, re-reads the committed database
  generation, and resolves the journal accordingly. A post-commit cleanup failure
  therefore keeps the candidate, while a failed commit still restores the prior
  token. If immediate reconciliation fails, the journal remains for the existing
  startup recovery path.

### Final invariant and quality checks

- Resolution is generation-aware: only a coordinated journal whose UUID matches
  the committed `WhoopConnection.token_generation` selects the candidate;
  mismatch or no committed generation selects the previous token. Repeating
  `resolve()` after journal cleanup is harmless, so recovery is idempotent.
- Lock ordering remains operation flock, then database or token lock. Exception
  reconciliation runs only after the replacement context has released the token
  lock, avoiding the earlier self-deadlock shape while still excluding a
  concurrent auth/sync/status operation for that account.
- The focused regression models a database generation becoming committed before
  session cleanup raises and verifies candidate retention and journal cleanup.
  The existing commit-failure regression now verifies old-token restoration via
  the same authoritative reconciliation path.
- Restart tests cover interruption after commit and injected failures at token
  `fchmod`, file `fsync`, replace, token-directory `fsync`, journal cleanup, and
  journal-directory `fsync`. In every tested finalization fault, a fresh
  `TokenStore` uses the committed generation to recover the candidate. The
  pre-commit/post-replace failure test continues to recover the previous token.
- No token, credential, upstream body, profile identity, or exception content was
  added to CLI/log output. The new implementation emits no secret values; test
  token strings are synthetic. The delta does not alter OAuth HTTP behavior,
  scope enforcement, rate-limit handling, migration lineage, normalization, or
  profile/account isolation.
- The implementation report claims 151 full-suite passes, 90 focused WHOOP
  passes, clean Ruff/mypy/diff gates, and the existing migration checks. Per
  instruction, this review inspected the code, tests, report, and diff only and
  did not rerun those gates.

### Live-only concerns

- The configured WHOOP Developer application, exact loopback redirect, browser
  callback, authorization-code exchange, and first full sync still require the
  planned human acceptance run. No real credential or account payload was used in
  this round.
- Real history volume, upstream response variation/eventual consistency,
  pagination, refresh-token rotation, reduced live scopes, and actual WHOOP
  rate-limit headers remain unexercised outside mocked/local coverage.
- Crash tests provide strong injected-fault evidence, but power-loss durability
  still ultimately depends on atomic rename and `fsync` behavior on the target
  macOS filesystem.
- The migration conclusion still depends on the documented pre-release fact that
  no real database retained the earlier `0005_whoop`; any retained review database
  must be confirmed data-free and rebuilt as documented.
