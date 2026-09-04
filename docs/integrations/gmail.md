# Gmail medical ingestion

## TL;DR

The connector is mocked-ready, read-only, multi-profile, and multi-account. A
first scan examines the configured lookback (seven days by default), excluding
Spam and Trash; later scans resume from Gmail `historyId` and process relevant
label transitions. Medical PDFs enter the same PostgreSQL/import/review pipeline
as local files. Recognized body-only medical mail enters a content-free common
source inbox, while appointments and files needing OCR remain visible through
safe internal attention status without prompting in Telegram.

Live activation still requires a Google Desktop OAuth client and one browser
authorization per account. That authorization is durable only when the actual
OAuth consent screen is **Production** (External) or **Internal** for an eligible
Workspace organization. External/Testing refresh tokens using `gmail.readonly`
expire after seven days.

## Data flow and safety boundary

- OAuth requests exactly `https://www.googleapis.com/auth/gmail.readonly`; no
  send, modify, trash, delete, or label mutation is exposed.
- The first query is `newer_than:7d -in:spam -in:trash`, configurable from
  1–365 days. Incremental history includes added/deleted messages and label
  changes; current `SPAM`/`TRASH` labels are rejected, while restored mail is
  reconsidered. Full scans and history-404 recovery also refetch all previously
  known medical/attention message IDs before committing the new cursor, so a
  missed deletion or Spam/Trash transition is reconciled.
- Message subject, filename, and a bounded in-memory body prefix identify
  appointments and conservative medical signals. For a recognized body-only
  message, PostgreSQL receives only an idempotent profile-scoped `SourceRecord`
  containing Gmail IDs/type/source link; body text and sender are never persisted
  or logged. The item remains in `gmail attention` for later agent handling. This
  is routing, not full medical interpretation of arbitrary email prose.
- PDF/JPEG/PNG/TIFF/HEIC/HEIF/WebP candidates are incrementally decoded from the
  complete base64url value returned by Gmail. This is not end-to-end network
  streaming: Gmail's client library materializes the encoded API response.
- Declared size is checked before attachment download. Encoded and decoded hard
  limits, exact decoded size, and file magic/MIME are checked in a private staged
  file before the medical importer is called. Default maximum is 25 MiB.
- Metadata-ambiguous PDFs are locally content-classified. Medical/scanned PDFs
  enter `import_document` with `source_provider="gmail"`, stable Gmail
  occurrence identity, source link, profile ownership, content hash, and the
  normal lab review queue. Nonmedical PDFs are discarded after classification.
  Images are safely staged with outcome `ocr_required` and reason
  `image_ocr_required` until the shared image OCR path exists; they are never
  reported as medically imported. Both run and lifetime status expose the OCR
  count and `gmail attention` exposes only safe IDs plus the reason.
- Supported MIME attachments without filenames are accepted when Gmail supplies
  attachment identity or an attachment disposition. They receive a deterministic
  hash-derived local filename before validation/import; no Gmail identifier is
  used as a filesystem path.
- Sync is serialized by a cross-process profile/account lock. Item state is
  fsynced before its cursor. Delivery is intentionally **at least once** across a
  process crash; the common content-addressed importer and stable occurrence
  identity make retries idempotent. Immutable attachment revisions are retained.
- Private config/token/state files are `0600`, directories are `0700`, and
  symlinked path components are rejected. Token identity and credentials are
  published together only after `users.getProfile` verifies the mailbox; a
  failed or wrong-account reauthorization preserves the old token.

## Google OAuth setup

1. Enable Gmail API in the Google Cloud project and create an OAuth client of
   type **Desktop app**. Save its JSON to the ignored
   `data/secrets/google-oauth-client.json`.
2. Choose the real consent-screen mode:
   - **External / Testing** is suitable only for setup/testing. Add test users,
     set `GOOGLE_OAUTH_PUBLISHING_STATUS=testing`, and expect reauthorization
     after seven days.
   - **External / Production** is required for unattended personal Gmail use.
     Publish the consent screen, satisfy Google's current restricted-scope
     verification requirements for the configured audience/use, and set
     `GOOGLE_OAUTH_PUBLISHING_STATUS=production`.
   - **Internal** is available only to an eligible Google Workspace
     organization; set `GOOGLE_OAUTH_PUBLISHING_STATUS=internal`.
3. Configure and authorize each account slot. The setting is a local declaration
   shown by status; the connector cannot query the Cloud project's publishing
   status, so it must match the Console.

For the first live run, point `DATABASE_URL`, `VAULT_ROOT`, `TEMPORARY_ROOT`, and
`GMAIL_ROOT` at dedicated local staging locations. OAuth client and connector
roots are also Settings/env-overridable; no production path is hardcoded.

Refresh tokens can also stop working after revocation, a Gmail-password change,
long inactivity, account token limits, or admin policy. A refresh failure is
persisted as `oauth_required`; `gmail status` then says `reauth_required` even if
the stale token file still exists. Every sync invocation records a fresh preflight
attempt timestamp; failures preserve the last successful-sync timestamp.

## Commands

```bash
uv run health-agent gmail configure PROFILE_UUID personal
uv run health-agent gmail auth PROFILE_UUID personal
uv run health-agent gmail sync PROFILE_UUID --account-id personal
uv run health-agent gmail status PROFILE_UUID
uv run health-agent gmail attention PROFILE_UUID
```

Omit `--account-id` to status/sync all configured account slots independently.
Use `gmail sync PROFILE_UUID --full` to repeat the lookback and retry internally
queued file revisions after configuration or OCR capability changes. Status
separates staged, medically imported, attention, cursor freshness, last attempt,
last success, safe error, token state, and declared OAuth mode. A missing profile
or requested account is an expected status state: the command prints
`status=not_configured action_required=configure` and exits successfully.

## Official references

- [Gmail Python quickstart](https://developers.google.com/workspace/gmail/api/quickstart/python)
- [Gmail OAuth scopes](https://developers.google.com/workspace/gmail/api/auth/scopes)
- [Google refresh-token expiration](https://developers.google.com/identity/protocols/oauth2#expiration)
- [OAuth policies](https://developers.google.com/identity/protocols/oauth2/policies)
- [List Gmail messages](https://developers.google.com/workspace/gmail/api/guides/list-messages)
- [Synchronize Gmail clients](https://developers.google.com/workspace/gmail/api/guides/sync)
- [Gmail history semantics](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.history/list)
- [Message and MIME resource](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages)
- [Attachment body semantics](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages.attachments)
- [Error and retry guidance](https://developers.google.com/workspace/gmail/api/guides/handle-errors)
