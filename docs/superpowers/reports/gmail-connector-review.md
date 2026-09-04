# Gmail connector review

Review target: `codex/v1-gmail` at `e084768`, relative to `305b837`.

## Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

The Gmail API adapter is a useful mocked foundation, but the delivered CLI is not
yet the approved Gmail-to-health-record integration. Findings 1–6 are blockers for
calling the connector live-ready or enabling unattended sync.

## Findings

### 1. High — `gmail sync` does not import anything into the medical database

The production CLI injects `VaultAttachmentImporter`
(`src/health_agent/cli.py:281-292`). That adapter only writes bytes into a separate
profile/account vault and returns a receipt
(`src/health_agent/gmail/vault_importer.py:16-66`); it never calls the existing
profile-aware `import_document`, creates `SourceRecord`/
`DocumentSourceRecord`/`Document`, extracts lab values, or puts them in review.
Nevertheless, the CLI prints `status=synced ... imported=N` at
`src/health_agent/cli.py:300-315`.

As a result, a successful real run leaves the attachment unavailable to the agent,
review queue, and dashboards, and uses a vault namespace that does not deduplicate
with existing Drive/local/Telegram documents. The integration guide candidly calls
the database adapter future work (`docs/integrations/gmail.md:45-49`), but README
says the Gmail connector is implemented. Wire the real importer now, retain Gmail
source provenance in PostgreSQL, and distinguish staged bytes from medically
imported documents in reports/status. No new migration is required to use the
existing schema.

This directly conflicts with the approved design, which makes PostgreSQL the
source of truth and requires Gmail documents to enter the common medical pipeline
(`docs/superpowers/specs/2026-09-04-personal-health-agent-v1-design.md:96-105,
153-157`).

### 2. High — the classifier systematically strands valid medical mail

The initial query is attachment-only
(`src/health_agent/gmail/service.py:128-144`), and `_process_message()` considers
only MIME parts classified as attachments (`:179-212`). Appointment confirmations
or medical results contained only in the message body are therefore ignored,
although the approved v1 explicitly includes appointment confirmations. Generic
PDF/images with no matching English/Russian filename or subject token and no
manually trusted sender become metadata-only `ambiguous`; their bytes are never
inspected (`:255-262`). On later scans the same revision is skipped permanently
when its classification has not changed (`:243-253`).

There is no autonomous second-stage classifier and no usable ambiguity-list or
resolution command. The only recovery is for an operator to inspect private JSON
manually, infer the sender (which is not retained on the ambiguity record), and
configure it as trusted. This avoids Telegram questions but does not satisfy the
user requirement that the system itself recognize analyses, reports, studies, and
appointments. Add a privacy-preserving local/content classification stage and a
durable internal retry/review path; keep non-medical bodies/bytes after the
decision only if policy allows.

### 3. High — the documented “one-time” OAuth setup expires after seven days in the recommended mode

The runbook instructs the owner to keep the personal External OAuth app in
**Testing** and add accounts as test users (`docs/integrations/gmail.md:21-28`),
while both the runbook and report call authorization a one-time action. Google
officially documents that External/Testing refresh tokens expire after seven days
unless only basic identity scopes are requested. `gmail.readonly` is a restricted,
non-basic scope, so the documented setup requires weekly authorization and cannot
support unattended daily ingestion.

Document a durable personal-production/internal configuration and its verification
trade-offs. Detect refresh expiry as `oauth_required`, persist it in source status,
and stop claiming one-time setup under Testing. Current sync catches the refresh
exception only long enough to print its class, while the stale token file continues
to make status say authorized.

Official references:

- <https://developers.google.com/identity/protocols/oauth2#expiration>
- <https://developers.google.com/workspace/gmail/api/auth/scopes>

### 4. High — reauthorization destroys the previous binding before identity verification

`gmail auth` always uses `force=True`; `GmailOAuth.authorize()` publishes the new
token at `src/health_agent/gmail/oauth.py:25-49` before the CLI calls
`users.getProfile` (`src/health_agent/cli.py:208-224`). If an already-bound slot for
Alice is accidentally authorized as Bob, the new token first replaces Alice's
working token, the mismatch is discovered, and `tokens.clear()` then deletes the
only token file. A transient profile-call failure likewise leaves an unverified
replacement token in a slot still labelled as the old mailbox.

Stage credentials, verify the returned email and local profile/account binding,
then atomically publish both token and binding. Preserve/restore the old token on
any failure. Also prevent the same mailbox from being silently attached to two
different person profiles unless that is an explicit supported policy.

### 5. High — the JSON state store can lose records while advancing the cursor

Each attachment, message, removal, and cursor operation independently reads the
entire account JSON and atomically replaces it
(`src/health_agent/gmail/stores.py:114-203`). There is no per-account lock,
generation/CAS check, or transaction. Two overlapping manual/scheduled syncs can
both read state N, write different records, overwrite one another, and finally
publish a newer `historyId` despite losing the other process's attachment
provenance. Atomic rename prevents torn JSON; it does not prevent lost updates.

Serialize sync per profile/account across processes or move cursor/decisions into
transactional profile-scoped database storage. Add an overlapping-sync test that
proves records cannot be lost before a cursor advances.

### 6. High — initial and incremental scans cover different mail populations

The initial `messages.list` explicitly excludes Spam and Trash
(`src/health_agent/gmail/api.py:105-118`), but incremental history requests filter
only `messageAdded`/`messageDeleted` and discard all label state
(`:143-166`). Thus a newly added spam message with medical-looking metadata can be
downloaded/imported incrementally even though the full scan excludes it. Conversely,
a recent message moved from Spam/Trash into the normal mailbox produces label
changes, not `messageAdded`, and will be missed because label history is filtered
out. Google documents `messageDeleted` as permanent deletion, not moving to Trash.

Carry `labelIds`, apply the same accepted-mail policy in both modes, and process the
relevant label transitions (or intentionally repeat the approved sliding seven-day
query each day). This is both a completeness and malicious-attachment boundary.

Official history semantics:
<https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list>

### 7. Medium — attachment “streaming” and validation happen too late

`attachments.get` returns the complete base64url string and the gateway materializes
it in memory (`src/health_agent/gmail/api.py:168-182`). The decoder then yields
chunks, but there is no decoded-size limit and no byte-signature validation against
the declared/effective MIME type. A mislabeled octet-stream file is trusted solely
by filename. The Gmail-declared size is checked only after the injected importer
has completed its side effect (`src/health_agent/gmail/service.py:264-304`); on a
mismatch the vault object exists but no attachment state is recorded, so every
retry repeats the import attempt.

Describe this as incremental decode rather than end-to-end network streaming, set
an explicit accepted size bound, sniff supported file signatures before medical
parsing, and make staging/size verification precede the committed importer action.
The included vault adapter does correctly compute SHA-256 and verify its
content-addressed receipt.

Official body semantics:
<https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments>

### 8. Medium — “once per revision” and durable provenance are overstated

The importer and JSON state are not one transaction. If an injected importer
succeeds and `record_attachment()` then fails, the cursor remains old and the same
revision is delivered again. That is at-least-once delivery; only an independently
idempotent importer makes it effectively once. The protocol passes a stable
revision, but does not state/enforce that requirement.

Moreover, attachments are keyed only by `message_id:part_id`
(`src/health_agent/gmail/stores.py:156-170,215-216`), so a changed attachment ID
overwrites the prior revision rather than retaining revision history. `SeenAttachment`
also drops the provenance object's thread ID, message history ID, internal date,
account email, and source URI (`src/health_agent/gmail/types.py:53-98`); the included
vault importer does not persist them elsewhere. Preserve immutable occurrence
revisions and make the importer idempotency contract explicit and tested.

### 9. Medium — status is existence-based and has no freshness or failure history

`gmail status` prints `authorized=yes` when a path merely exists
(`src/health_agent/cli.py:231-256`); it does not parse the token, verify exact scopes,
test refreshability, or reject a symlink. It has no last-attempt, last-success,
last-error, or last cursor value/time, and errors printed by `sync` are not persisted.
The `imported` count currently means vault staging rather than medical import.

Report separately: configured, token-file present, token valid/reauth required,
cursor/freshness, staged, medically imported, ambiguous, and last safe error. This
is necessary for autonomous operation and a truthful future source dashboard.

### 10. Medium — pagination and transport failure hardening are incomplete

Neither the lookback nor history loop rejects a repeated `nextPageToken`, so a bad
or replayed response can spin forever. The five-attempt retry covers selected
`HttpError` statuses/reasons but not transport/timeouts, and the constructed Google
client has no explicit request timeout. Google's current error guide recommends
exponential retry for failures related to rate limits, network volume, or response
time. Add bounded transport handling, a timeout, and repeated-token tests while
continuing not to retry permanent 4xx errors.

Official reference:
<https://developers.google.com/workspace/gmail/api/guides/handle-errors>

### 11. Low — parent-directory and test coverage claims are weaker than file permissions

Files written successfully are `0600` and created directories are chmodded `0700`,
but `_private_directory()` follows existing directory symlinks without an `lstat`
check (`src/health_agent/gmail/stores.py:20-32`). That can redirect one profile or
account's token/state tree outside the intended root. For the local non-enterprise
threat model this is not a remote exploit, but it undercuts the asserted filesystem
isolation and is easy to harden consistently with file checks.

The 37 Gmail tests are well isolated in `tmp_path` with mocked OAuth/API adapters
and no Gmail database/network access. They do not directly cover profile/list/
attachment gateway parameters, OAuth state/callback, reauthorization rollback,
repeated page tokens, concurrency, real importer integration, spam/label behavior,
size mismatch after side effects, or truthful status on corrupt/symlink tokens.

## Confirmed behavior

- The only requested scope is exactly
  `https://www.googleapis.com/auth/gmail.readonly`; the adapter exposes only read
  calls. Broader persisted scope sets are rejected. Google currently classifies
  this scope as restricted.
- OAuth authorization/state/callback validation is delegated to the official
  `google-auth-oauthlib` Installed App flow. It uses a loopback server with a random
  port and requests offline access. The library flow is not exercised end-to-end by
  branch tests, and `run_local_server` is invoked without a timeout.
- Successful token/config/state writes are temporary-file + `fsync` + atomic
  replace with `0600` files and `0700` directories. The default roots and client
  secret are ignored by Git. Plain private files are proportionate to the agreed
  local-Mac, non-enterprise boundary.
- The initial query is exactly `newer_than:7d has:attachment` by default, pages at
  Gmail's documented maximum of 500, and captures a pre-scan mailbox history ID so
  arrivals during the scan can be replayed idempotently later.
- Incremental history is accumulated across pages before processing. The cursor is
  advanced only after every selected message/removal completes; partial importer or
  state failures leave the old cursor. An expired-history 404 triggers the
  configured lookback, consistent with Google's synchronization guide.
- Nested MIME traversal, RFC header decoding, external and inline body-data forms,
  strict incremental base64url decoding, declared decoded-size comparison,
  SHA-256 in the included vault adapter, and local removal tombstones are present.
- Profile UUID/account path scoping and bound-email verification prevent an already
  bound token from being used silently as another mailbox during sync. Same Gmail
  message/part IDs in different profile/account stores do not collide.
- Ignored message bodies and attachment bytes are not durably retained; subject,
  sender, and base64 data are hidden from dataclass reprs and are not printed by the
  CLI. Stored ambiguity metadata and imported files remain local/private.
- The same Google Desktop client JSON can be reused for Drive when both APIs are
  enabled. Gmail uses a separate per-profile/account token and disables incremental
  scope union, so this implementation does not require a combined Gmail+Drive
  token or broaden Drive access.
- README, integration guide, and SDD report disclose mocked testing and the absent
  live mailbox. The integration guide also discloses that the production database
  importer/OCR handoff is future work, although README/CLI terminology is broader.

Official references checked on 2026-09-04:

- <https://developers.google.com/workspace/gmail/api/auth/scopes>
- <https://developers.google.com/workspace/gmail/api/guides/sync>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages>
- <https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments>
- <https://developers.google.com/workspace/gmail/api/guides/handle-errors>
- <https://developers.google.com/identity/protocols/oauth2/native-app>
- <https://developers.google.com/identity/protocols/oauth2#expiration>

## Live-only assumptions and handoff gaps

- No Google Cloud project, consent-screen mode, restricted-scope warning, browser
  callback, refresh, real Gmail account, large attachment, or real 404/rate-limit
  response has been accepted end-to-end.
- No daily scheduler is delivered here; the callable is manual CLI. The JSON store
  also needs serialization before a scheduler can safely overlap with manual runs.
- Gmail search behavior, historical mailbox volume, foreign-language metadata,
  real MIME edge cases, and multi-login Gmail source links remain synthetic-only.
- No image OCR path or PDF medical-database path is connected, so real imported
  documents cannot yet complete the required Gmail-to-review/dashboard scenario.

## Pre-existing migration issue (not caused by this branch)

The implementation report records that `alembic downgrade base` fails in the
pre-existing `0003_review_corrections` downgrade because a recreated
`verified_lab_history` view still depends on the column being removed. This branch
adds no Alembic/model changes. The issue remains real in the base migration chain,
but it is not counted as a Gmail branch finding or in the Gmail verdict.

## Review method

Read the approved v1/multi-profile/autonomy design, connector plan, implementation
report, integration guide, branch diff, current code, and tests. Official behavior
was checked against current Google Gmail and OAuth documentation. Per instruction,
this review changed no implementation and did not rerun tests; the reported 37/98
test and lint/type/migration results are implementation-report evidence, not
independently reproduced here.

## Fix round 1 re-review

Review target: `codex/v1-gmail` at `129eb44`, with the original review retained
in `2f6163e`. This round inspected the fix diff and existing reported evidence;
tests were not rerun, as requested.

### Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

Most of the connector foundation is now materially stronger. The production CLI
uses the common profile-aware PostgreSQL importer for PDFs, Gmail occurrence
identity is stable and idempotent, token replacement is staged and verified,
profile/account sync is protected by a cross-process lock, current labels make
incremental Spam/Trash handling symmetric, attachment limits and magic checks
precede importer effects, API/OAuth waits are bounded, and the `0003` downgrade
now removes and restores the dependent view safely. The official Gmail/OAuth
references still support the selected read-only scope, loopback installed-app
flow, history semantics, and the documented seven-day External/Testing token
limitation.

The remaining findings below prevent the branch from truthfully claiming that
the approved autonomous Gmail medical-ingestion behavior is complete.

### Findings

#### 1. High — body-only medical mail still does not enter the common pipeline, and appointment attention cannot be listed

`classify_message()` recognizes only appointment vocabulary
(`src/health_agent/gmail/classifier.py:65-89`). A body-only lab result, radiology
result, discharge summary, or other medical message is still classified as
ignored even though the approved design requires analyses, conclusions,
investigations, and appointment confirmations from Gmail to enter the common
medical pipeline. A recognized body-only appointment is only written as a
minimal `SeenMessage` in connector JSON (`src/health_agent/gmail/service.py:240-264`);
it produces no common-database source/event and retains no usable appointment
details.

Even that minimal appointment record is not exposed by the advertised internal
queue. `gmail attention` calls `attention_items()`, which iterates only attachment
records (`src/health_agent/cli.py:278-290`,
`src/health_agent/gmail/stores.py:360-370`). Therefore the report's claim that
body-only appointments have a “safe internal attention listing” and the guide's
claim that they remain visible are false: only the aggregate attention count
changes. Complete the body-only medical/appointment handoff, or explicitly narrow
the scope and make queued messages listable/actionable without retaining arbitrary
non-medical bodies.

#### 2. Medium — full/recovery scans can advance past removals without reconciling existing state

Incremental sync now correctly fetches current labels and sees label transitions.
However, `--full` and expired-history recovery list only the positive seven-day
query and then replace the cursor (`src/health_agent/gmail/service.py:151-175`).
They do not reconcile previously recorded messages that are absent because they
were deleted or moved to Spam/Trash. A manual full scan, or an automatic recovery
after a history `404`, can consequently advance beyond the only removal/label
event while leaving the old local message and attachment statuses active forever.

This is a residual data-truth gap in the promised removal semantics. Recovery
needs a bounded reconciliation strategy for already-known active message IDs (or
the documentation/status model must explicitly describe removal information as
lost when Gmail history expires). Add regression coverage for a known message
moved to Trash/deleted immediately before full scan and before cursor-expiry
recovery.

#### 3. Medium — OCR and attention status reporting is not truthful end to end

The image adapter returns `outcome="needs_attention"` and separately sets
`processing_status="image_ocr_required"`
(`src/health_agent/gmail/medical_importer.py:54-62`). The service persists only
the generic outcome and drops `processing_status`; its OCR counter increments
only for `outcome == "ocr_required"`
(`src/health_agent/gmail/service.py:335-374`). Thus a supported image queued
specifically for OCR prints `ocr_required=0`, is listed with
`reason=needs_attention`, and cannot later be distinguished from other attention
causes. In addition, `gmail status` does not print an OCR count at all
(`src/health_agent/cli.py:256-275`), despite the guide/report claiming that status
separates OCR from attention.

Persist the processing reason (or use `ocr_required` consistently as the outcome)
and expose consistent lifetime/run counts. Add a CLI-level image test, not only an
adapter receipt assertion.

#### 4. Low — repeated OAuth/preflight failures leave `last_attempt` stale

Preflight failures bypass `begin_sync()` and call `fail_sync()`, but
`fail_sync()` preserves any existing `last_attempt_at` instead of setting the time
of the current failed attempt (`src/health_agent/cli.py:305-353`,
`src/health_agent/gmail/stores.py:258-263`). After one prior run, repeated expired
token or other preflight failures therefore make the freshness field lie. Update
the attempt timestamp on every invocation while preserving `last_success_at`.

#### 5. Low — supported unnamed MIME attachments are permanently ignored

Any supported PDF/image part without a filename is classified as ignored before
content inspection (`src/health_agent/gmail/classifier.py:98-102`), even if Gmail
marks it as an attachment and supplies `attachmentId` or inline data. This is a
valid MIME shape and creates a completeness hole for generic-content routing.
Use disposition/attachment identity plus validated magic as the fallback, and
cover an unnamed PDF attachment.

### Original-finding disposition

- **Resolved:** common PDF database importer/provenance/idempotency; truthful
  `medically_imported` versus duplicate outcomes for that path; External/Testing
  OAuth documentation; staged verified reauthorization and old-token retention;
  cross-profile mailbox binding; cross-process state/cursor serialization;
  incremental Spam/Trash/restored-mail behavior; pre-import size/hash/magic/MIME
  validation; bounded transport and OAuth callback waits; retry/page-loop guards;
  immutable attachment revisions/full provenance; token/state symlink hardening;
  and the pre-existing `0003` downgrade dependency.
- **Partially resolved:** autonomous body/generic classification, internal
  attention visibility, truthful OCR/status reporting, and full/recovery removal
  semantics, as detailed above.

### Remaining live concerns

- No live Google consent screen, callback, token refresh, mailbox, large MIME
  payload, history-expiry response, rate-limit response, or real PostgreSQL/vault
  ingestion has been accepted in this branch. The documented publishing mode is
  deliberately a local declaration and cannot verify Google Cloud Console state.
- The Google client library necessarily materializes the attachment API's encoded
  response before the connector's bounded incremental decoder runs; the guide now
  states this accurately.
