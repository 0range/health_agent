# Google Drive connector

## TL;DR

The connector is tested with mocked Google APIs and disposable PostgreSQL. It
reads private My Drive folders recursively, sends PDFs through the common
medical database/review pipeline, routes scans to attention, and never calls a
Drive write method. Live acceptance still requires a Desktop OAuth client and
explicit authorization for each person.

## What the Drive folder needs

- It can remain private; the connected Google account only needs normal read access.
- Paste one or more folder links or IDs per local profile.
- Any nesting and filenames are allowed; traversal is recursive.
- PDF, JPEG, PNG, TIFF, HEIC/HEIF, and WebP files are downloaded as binary streams.
- Google Docs, Sheets, Slides, and Drawings are exported to PDF when Google permits download. Other Google-native types are recorded as unsupported rather than guessed or modified.
- Drive shortcuts are skipped in v1 because their targets are not descendants of the configured medical folder; add the target folder itself when it should be imported.
- Download-disabled items are recorded with a safe status and left unchanged.
- Repeated scans compare the Drive file ID and revision before downloading; content SHA-256 and size are preserved after download.
- Shared-drive roots are rejected in v1. Their independent change logs need a
  separate cursor model; silently treating them like My Drive would lose data.
- Every supported, unsupported, restricted, oversized, corrupt, removed, or
  failed item gets a local machine status. One bad file does not stop later files.
- Exhausted transient/processing failures remain in a private durable retry queue
  and are replayed by the next ordinary incremental run before its cursor advances.
- Explicit `Collection date` / `Дата забора` is used for laboratory charts;
  explicit issue/report date is the fallback. Drive created/modified timestamps
  are provenance only and are never substituted for a medical date.
- The review queue shows the document ID and detected dates. A reviewer can set
  or correct either date before approval:

  ```bash
  uv run health-agent review set-date DOCUMENT_ID --collected-date YYYY-MM-DD
  ```

  Use `--issued-date YYYY-MM-DD` for an issue date.

Google's API limits Google Workspace exports to 10 MB. Such an item is recorded
as `too_large`; convert it to a regular PDF for ingestion.

## One-time Google setup

1. In one Google Cloud project, enable the Drive API and configure the OAuth consent screen.
2. Create an OAuth client with application type **Desktop app**. Add each Google
   account as a test user while configuring it. Important: an External app left
   in **Testing** normally issues a refresh token that expires after seven days
   for this Drive scope. Durable background sync needs a published Production
   app (or an Internal Google Workspace app); otherwise reauthorization is
   expected.
3. Save the downloaded JSON as `data/secrets/google-drive-oauth-client.json`. Both `data/` and tokens are ignored by Git; the connector enforces local file mode `0600`.

The requested scope is exactly `https://www.googleapis.com/auth/drive.readonly`.
Google grants it read access to every file visible to that account, although the
connector ingests only configured roots. It cannot create, edit, move, delete,
rename, or share files.

## Commands

```bash
uv run health-agent drive configure PROFILE_ID 'GOOGLE_DRIVE_FOLDER_URL'
uv run health-agent drive auth PROFILE_ID
uv run health-agent drive sync PROFILE_ID
uv run health-agent drive status PROFILE_ID
```

`PROFILE_ID` is the UUID printed by `health-agent profile list`, not a nickname.
Changing configured roots automatically invalidates the old cursor and forces a
full inventory. Use `--full` for periodic safety reconciliation; normal sync
resumes from the profile's My Drive Changes cursor.

## What status means

`drive status` is deliberately local and content-free. It separately reports
token state, stable account binding, whether roots passed the last successful
sync, last success/error, and per-item medical/attention counts. It never calls a
token file merely “authorized” based on file existence.

No new database migration is required: the production consumer uses the existing
profile-aware importer and immutable `SourceRecord` provenance with provider
`google_drive`, Drive file ID, revision, and link. Identical bytes deduplicate
only within one profile; two people remain separate.

Drive API and token-refresh calls use the finite timeout configured by
`GOOGLE_DRIVE_HTTP_TIMEOUT_SECONDS` (30 seconds by default). Root replacement and
cursor invalidation share the same per-profile process lock as synchronization,
so a running old-root scan cannot restore a stale cursor.

## Official references

- [Python Drive quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Drive OAuth scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)
- [Shared drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [Usage limits and retry guidance](https://developers.google.com/workspace/drive/api/guides/limits)
