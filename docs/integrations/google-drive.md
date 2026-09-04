# Google Drive connector

## TL;DR

The connector code is ready and tested without real credentials. It reads private folders recursively, keeps each person's account, folders, token, cursor, and downloaded vault separate, and never calls a Drive write method. One external setup remains: create a Google **Desktop OAuth client**, save its JSON locally, then authorize each profile once.

## What the Drive folder needs

- It can remain private; the connected Google account only needs normal read access.
- Paste one or more folder links or IDs per local profile.
- Any nesting and filenames are allowed; traversal is recursive.
- PDF, JPEG, PNG, TIFF, HEIC/HEIF, and WebP files are downloaded as binary streams.
- Google Docs, Sheets, Slides, and Drawings are exported to PDF when Google permits download. Other Google-native types are recorded as unsupported rather than guessed or modified.
- Drive shortcuts are skipped in v1 because their targets are not descendants of the configured medical folder; add the target folder itself when it should be imported.
- Download-disabled items are recorded with a safe status and left unchanged.
- Repeated scans compare the Drive file ID and revision before downloading; content SHA-256 and size are preserved after download.

Google's API limits Google Workspace exports to 10 MB. A source file above that limit must be converted to a regular PDF by its owner before this v1 connector can ingest it.

## One-time Google setup

1. In one Google Cloud project, enable the Drive API and configure the OAuth consent screen.
2. Create an OAuth client with application type **Desktop app**. While this personal installation is in testing, add each Google account as a test user.
3. Save the downloaded JSON as `data/secrets/google-drive-oauth-client.json`. Both `data/` and tokens are ignored by Git; the connector enforces local file mode `0600`.

The requested scope is exactly `https://www.googleapis.com/auth/drive.readonly`. It can view and download Drive files but cannot create, edit, move, delete, rename, or share them.

## Commands

```bash
uv run health-agent drive configure PROFILE_ID 'GOOGLE_DRIVE_FOLDER_URL'
uv run health-agent drive auth PROFILE_ID
uv run health-agent drive sync PROFILE_ID
uv run health-agent drive status PROFILE_ID
```

Use `drive sync PROFILE_ID --full` to deliberately rebuild inventory. Normal sync resumes from the per-profile Drive Changes cursor.

## Integration boundary

This branch intentionally adds no database migration. `DriveService` depends on explicit `SyncStateStore` and `ContentConsumer` protocols, while `DriveProvenance` carries profile, root, path, Drive file ID, version/revision, timestamps, link, source MIME, output MIME, SHA-256, and size.

When integrated on top of migration `0004_chart_integrity`, the production consumer should map the local profile key to `profiles.id`, then call the profile-aware medical importer with `source_provider="google_drive"`, `source_external_id=<Drive file ID>`, and the source link. The existing `documents(profile_id, sha256)` and `document_source_records` constraints will deduplicate identical bytes within one profile while retaining Drive as another source occurrence. Connector cursor/revision state may initially remain in its private local JSON store; moving it into PostgreSQL can use the same protocol later.

## Official references

- [Python Drive quickstart](https://developers.google.com/workspace/drive/api/quickstart/python)
- [Drive OAuth scopes](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [Download and export files](https://developers.google.com/workspace/drive/api/guides/manage-downloads)
- [Retrieve changes](https://developers.google.com/workspace/drive/api/guides/manage-changes)
- [Shared drive support](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [Usage limits and retry guidance](https://developers.google.com/workspace/drive/api/guides/limits)
