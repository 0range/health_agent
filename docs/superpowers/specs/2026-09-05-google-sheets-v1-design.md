# Google Sheets v0.1 — design

Date: 5 September 2026

Status: approved for implementation as the Google Sheets vertical slice of the
Personal Health Agent v1 design.

## Outcome

Each health profile has one private Google spreadsheet with three useful tabs:

- `Lab history` is a generated view of verified laboratory observations;
- `Needs review` is a generated queue whose decision and correction cells are
  the only user inputs;
- `Sources` shows connector authorization, freshness, and safe operational
  status.

PostgreSQL remains the source of truth. The spreadsheet is a readable projection
and a narrow, explicitly validated review interface, not another medical database.

## Ownership and authorization

Sheets uses a dedicated OAuth token with exactly the Sheets and Drive `drive.file`
scopes. A read-only Drive token cannot be reused for writing. The existing Google
Desktop OAuth client, private-file conventions, retry strategy, and verified
Drive account binding are reused where safe.

`sheets configure PROFILE_ID` creates a profile-scoped local configuration. If
the profile already has a verified Drive binding, its Google permission ID and
email become the required Sheets account identity. `sheets authorize PROFILE_ID`
stages credentials, calls Google Drive `about` to verify the actual account, and
only then atomically publishes the token and binding. A different account fails
closed. A Google account cannot be bound to two local health profiles.

The first successful sync creates one spreadsheet and stores its ID and URL in
the profile configuration. A hidden `_HealthAgent` tab contains the profile UUID,
schema version, a random workbook binding token, and an initialization marker.
Every later sync verifies this binding before reading decisions or writing data.
The marker is committed atomically with the first projection, so a lost response
or local crash cannot erase a decision on restart. Ambiguous workbook creation is
durably fenced until explicit operator recovery. An existing arbitrary
spreadsheet is never adopted implicitly.

## Published data

`Lab history` contains only observations with `status=verified`, joined through
their document to the required profile. It contains the medical date, canonical
and source names, normalized and source values/units, laboratory reference,
document UUID, and a canonical Drive/Docs source link when safely available.
Local paths, arbitrary URLs, credentials, query strings, and fragments are
omitted. Rows without a medical date are
shown but clearly marked; no import timestamp is substituted. It excludes page
text, evidence excerpts, vault paths, API payloads, OAuth data, and credentials.

`Needs review` contains only unresolved observations from the required profile.
Stable machine columns hold review item ID, observation ID, profile ID, and a row
version hash over the immutable review payload. Visible evidence is restricted to
the parsed analyte/value/unit, reference, medical date, confidence, reason, and
source link. User-editable columns are:

- `Decision`: blank, `approve`, `correct`, or `reject`;
- `Corrected value`;
- `Corrected unit`;
- `Corrected canonical name`.

`correct` requires a value and uses the existing normalization allow-list.
`approve` and `reject` require correction cells to remain empty. Rows cannot
change profile, IDs, row version, or source evidence.

`Sources` contains safe connector state only: source type, local account label,
authorization status, last attempt/success, next retry, last safe error, and a
freshness label. It contains no tokens, raw provider response, message body,
attachment name, or extracted medical text.

## Synchronization

A profile-level local file lock serializes configure, authorization and sync.
Database row locks serialize every review transition, including non-Sheets
callers, and the row version is re-rendered under that lock. A sync performs these
steps in order:

1. verify the local profile, Sheets configuration, OAuth scopes, Google account,
   spreadsheet ID, and hidden workbook binding;
2. read literal/formula-preserving review cells, reject formulas, and validate
   the complete grid's schema, uniqueness, ownership, row versions, decisions,
   and correction shapes without changing the database;
3. apply the entire valid decision batch in one database transaction through the
   existing approve/correct/reject functions and append immutable audit rows;
4. rebuild all three projections from committed local data;
5. atomically replace the managed tab contents and formatting with one Google
   Sheets `batchUpdate` request;
6. record a safe successful sync run.

Malformed input, duplicate or unknown IDs, stale versions, already-resolved
conflicts, account mismatch, or workbook mismatch aborts the run before any
review decision is applied. If the remote write fails after the local transaction,
the next sync deterministically rebuilds the workbook from PostgreSQL. Repeated
identical decisions are recognized by a decision hash and are harmless; a
different replay for the same row fails closed.

No sync deletes or changes source medical records. Rewriting is limited to the
four sheets owned by the connector. Human edits outside the review input columns
are not imported and are overwritten on the next successful projection.

## Persistence and audit

Profile configuration, token, workbook binding, creation fence, lock, and
lightweight last-run state use the existing private atomic JSON/file-store pattern under
`data/google-sheets/PROFILE_ID/` with directories mode `0700` and files mode
`0600`.

PostgreSQL adds:

- `sheets_sync_runs`, with profile, status, timestamps, safe error, and aggregate
  counts;
- `sheets_review_decision_audits`, with profile, review item, observation,
  spreadsheet/row provenance, immutable row version, action, decision hash,
  correction fields, and applied time.

Audit rows never contain document bodies or evidence excerpts.

## CLI and automation

The public commands are:

- `health-agent sheets configure PROFILE_ID`;
- `health-agent sheets configure PROFILE_ID --reset-unknown-creation` after
  manually checking Drive for an orphaned ambiguous creation;
- `health-agent sheets authorize PROFILE_ID [--force]`;
- `health-agent sheets sync PROFILE_ID`;
- `health-agent sheets status PROFILE_ID`.

CLI output contains counts, identifiers safe for local administration, and stable
safe error codes; it never prints tokens, medical row values, document names, or
Google API error bodies. The existing automation registry discovers a configured
Sheets profile and runs `sheets sync PROFILE_ID`; missing OAuth is a deferred job,
not a failure that blocks WHOOP, Drive, or Gmail.

## Testing and non-goals

Tests use fake gateways and disposable PostgreSQL only; implementation does not
perform real OAuth, network calls, or access production health data. Coverage
includes multi-profile isolation, exact scopes and account binding, initial
workbook creation, idempotent projection, all three decisions, replay,
malformed/duplicate/stale/conflicting rows, remote write failure convergence,
status redaction, automation discovery, migration upgrade/downgrade, and CLI
composition.

Google Docs, arbitrary sheet layouts, formulas supplied by users, shared editing,
charts inside Sheets, and historical non-laboratory medical records are outside
this vertical slice. Metabase remains responsible for charts.
