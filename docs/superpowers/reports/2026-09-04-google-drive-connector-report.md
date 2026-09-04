# Google Drive connector implementation report

## Result

- Branch: `codex/v1-google-drive`
- Commit: the single connector commit at the tip of this branch.
- Scope: connector foundation only; no real Google account or medical file was used.

Implemented per-profile folder configuration, exact read-only OAuth, private local tokens/state, recursive paginated full inventory, Changes API incremental sync, transient retry handling, binary streaming, Google-native PDF export, explicit unsupported/download-restricted states, missing/removal reconciliation, and a profile-isolated vault adapter. CLI commands are `drive configure`, `drive auth`, `drive status`, and `drive sync`.

## Verification

- `uv run pytest -q`: 76 passed; only existing PyMuPDF SWIG deprecation warnings.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed.
- `git diff --check`: passed before commit.
- Credential-pattern scan found identifiers and documentation only, no credential values.

## Integration with main

No migration was added, so this commit can be applied after `0004_chart_integrity`. A production `ContentConsumer` should map the connector profile key to `profiles.id` and invoke the profile-aware importer with provider `google_drive`, Drive file ID, source link, and the profile-specific vault. `SyncStateStore` may stay local for v1 or be backed by future connector tables without changing `DriveService`.

## Only external setup still required

Create one Google Cloud **Desktop OAuth client**, enable Drive API, save the downloaded client JSON to the ignored `data/secrets/google-drive-oauth-client.json`, and run `drive auth` once for each profile. Live acceptance was intentionally not attempted without those user credentials.
