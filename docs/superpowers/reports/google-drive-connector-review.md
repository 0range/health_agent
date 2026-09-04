# Google Drive connector review

**Reviewed:** commit `f61ece5` against base `c7d4ea7`, the connector plan and implementation report, the approved v1 design, and the current multi-profile/management-panel design on the integration branch.

**SPEC verdict: FAIL (not ready to merge as the v1 Drive slice).** The branch is a useful connector foundation, but the executable CLI stops at a profile-local vault and does not put Drive files through the medical database/import/review pipeline. Several source-change cases can also be missed or misreported.

**QUALITY verdict: FAIL (not ready to merge without fixes).** The read-only API surface, local file permissions, recursive inventory, cursor commit ordering, bounded chunking, and basic profile-separated state are well structured. The remaining correctness and test gaps are material for unattended medical ingestion.

No tests or live Google authorization were run during this review, per review instructions. The `76 passed`, Ruff, and mypy results below are therefore prior results reported by the implementation author, not independently reproduced here.

## Blocking findings

### 1. `drive sync` does not import anything into the Health Hub, while the CLI reports `imported=N`

`sync_drive` injects only `FileVaultDriveConsumer` (`src/health_agent/cli.py:189-218`). That consumer writes content-addressed bytes and returns a receipt (`src/health_agent/google_drive/vault_consumer.py:16-51`); it never opens a database session, calls `import_document`, creates a `Document`/source occurrence, extracts a PDF, creates review items, or assigns an actionable document status. The only production `ContentConsumer` in the branch is this vault-only adapter.

Consequences:

- a Drive PDF/image cannot appear in the database, review queue, Sheets, or Metabase;
- the Drive timestamps and provenance remain only in mutable connector JSON;
- Drive removals never reach database provenance;
- supported images are counted as imported even though the existing medical importer is PDF-only;
- `status=synced ... imported=N` means “bytes placed in a vault,” not the user-visible import promised by v1.

The operator guide acknowledges that a production consumer is future work (`docs/integrations/google-drive.md:39-43`), but the README calls the connector ready (`README.md:11-13`) and the CLI wording is not truthful. Before merge, add a profile-aware production consumer that calls the existing importer transactionally, retains every source occurrence, maps output MIME correctly, and returns medical processing results; rename counters only if a deliberately vault-only staging mode remains.

### 2. Incremental sync is not correct for roots in shared drives

The gateway advertises shared-drive support in inventory (`supportsAllDrives` / `includeItemsFromAllDrives`, `src/health_agent/google_drive/api.py:112-137`) and accepts any accessible folder as a root, but it keeps only one user-level cursor (`src/health_agent/google_drive/types.py:141-145`) and calls `getStartPageToken` / `changes.list` without a `driveId` (`src/health_agent/google_drive/api.py:139-176`). Google documents that user and shared drives have separate change logs and that tracking all visible items requires replaying the user log plus every relevant shared-drive log. It also warns not to rely on user-log item events for members of a shared drive: <https://developers.google.com/workspace/drive/api/guides/about-changes>.

There is a second failure in the same path: a Change can have `changeType="drive"`, for which `fileId`/`file` are not the file payload. The partial-fields request does not request `changeType`, and `_parse` unconditionally indexes `value["fileId"]` (`src/health_agent/google_drive/api.py:154-170`). A shared-drive membership/change entry can therefore fail with `KeyError`. The official Change schema explicitly has both `file` and `drive` change types: <https://developers.google.com/workspace/drive/api/reference/rest/v3/changes>.

Either reject shared-drive roots explicitly in v1, or model cursors by `(profile, Google account, change-log/drive ID)`, request/branch on `changeType`, and test changes inside a shared-drive root plus drive membership/removal events.

### 3. Adding/changing configured folders can silently miss existing files

`drive configure` replaces the roots while preserving account, cursor, and seen state (`src/health_agent/cli.py:134-147`). Normal sync selects incremental mode whenever that old cursor exists (`src/health_agent/google_drive/service.py:124-129`). Files already present in a newly added root have no reason to occur after the old cursor, so they are invisible until the operator happens to know to pass `--full`.

Even a later full scan does not reliably refresh provenance: `_process_item` returns immediately for an unchanged file before updating its root, ancestor IDs, or path (`src/health_agent/google_drive/service.py:259-267`). A renamed/moved folder or changed root set can leave stale path/ancestor data and later removal reconciliation can act on the wrong tree.

Changing roots must invalidate the inventory/cursor or force a full reconciliation, and an unchanged content revision must still refresh location metadata. Tests should add a root after an existing cursor and move/rename a folder without changing child content.

### 4. One bad file can permanently block the whole source, and required machine statuses are missing

Download/export/consumer errors escape `_process_item` and abort the scan (`src/health_agent/google_drive/service.py:237-321`). This correctly avoids advancing the cursor, but it means every retry stops at the same bad item and later files never complete. In particular, the operator guide correctly notes that `files.export` is limited to 10 MB, but an oversized Google-native document gets no `too_large`/`needs_attention` status; it fails the whole sync. Google confirms both the 10 MB export limit and `capabilities.canDownload` precheck: <https://developers.google.com/workspace/drive/api/guides/manage-downloads>.

Unsupported regular files and shortcuts are merely counted as skipped and are not recorded at all (`src/health_agent/google_drive/service.py:176-180,245-257`), so the approved “whole archive has a status” behavior cannot be implemented from current state. Corrupt/OCR-required status is also impossible until finding 1 is fixed.

Record a safe per-item outcome and continue after non-systemic file failures; reserve run failure for auth/account/global API/state failures. Add exact cases for export-too-large, corrupt/consumer rejection, unsupported binary/native, download-restricted, and a later valid file still being processed.

## High-priority correctness findings

### 5. Incremental removal handling ignores trash state

Full traversal excludes `trashed=true`, but `_FILE_FIELDS` does not request `trashed` (`src/health_agent/google_drive/api.py:28-32`) and incremental processing treats every non-`removed` file payload as live (`src/health_agent/google_drive/service.py:193-226`). Google exposes `File.trashed` separately, while `Change.removed` describes removal from the change list, such as deletion or loss of access: <https://developers.google.com/workspace/drive/api/reference/rest/v3/files> and <https://developers.google.com/workspace/drive/api/reference/rest/v3/changes>. A file moved to trash can therefore remain/import again until a manual full scan.

Request and honor `trashed`, and add incremental trash/untrash tests. The management design also requires periodic safety reconciliation; this branch exposes only manual `--full`, so scheduling still has to be supplied by the integrated source runner.

### 6. Account binding and token persistence are not one safe operation

`DriveOAuth.authorize` persists credentials before the CLI looks up and verifies the Google account email (`src/health_agent/google_drive/oauth.py:25-47`; `src/health_agent/cli.py:150-165`). If the account lookup fails, an unbound profile is left with a token and `drive status` says authorized. If an already-bound profile authorizes a different account, the mismatch is raised only after the old good token has been overwritten by the mismatched token. A later sync is stopped by `verify_account`, but the installation now needs manual token repair. The binding itself uses mutable/display email only (`src/health_agent/google_drive/api.py:108-110`), not Drive `about.user.permissionId` or another stable external account ID required by the management model.

Acquire credentials into a temporary/pending token, resolve the stable Google account identity, validate the existing binding, and only then atomically commit both binding and token. Prompting with account selection or a login hint would also reduce wrong-account authorization on a two-person Mac. Tests need both “account lookup fails” and “mismatched reauthorization preserves the prior token/account.”

### 7. Connector profile keys are not yet the stable database ownership boundary

The connector validates an arbitrary local string (`src/health_agent/google_drive/config.py:12-23`) and stores a separate profile JSON; it does not verify that this key is an existing `profiles.id`. The integration guide delegates an unspecified mapping to the future consumer (`docs/integrations/google-drive.md:41-43`). That is not sufficient for the approved invariant that every document/source/sync run is owned by one stable profile and cannot cross profiles.

On integration, use the database Profile UUID (or a persisted, unique connector-account foreign-key mapping) at configuration, state, consumer, and importer boundaries. Fail closed when it is missing. Add a real database-backed test in which identical bytes and identical Drive IDs under two profiles produce separate documents/source ownership and never appear in the other profile’s query. The current memory/vault tests prove local path/key separation only (`tests/google_drive/test_service.py:214-228`; `tests/google_drive/test_stores.py:44-62`).

## OAuth, secrecy, API, and CLI observations

### 8. Scope/state are mostly sound, but the callback does not meet the explicit `127.0.0.1` requirement

Positive findings:

- the requested and persisted scope is checked as exactly `https://www.googleapis.com/auth/drive.readonly` (`src/health_agent/google_drive/config.py:10`; `src/health_agent/google_drive/oauth.py:25-46,49-68`);
- only Drive read methods are constructed; no Drive create/update/delete/move/share method appears in the connector;
- the pinned `google-auth-oauthlib` flow generates PKCE and OAuth state and `requests-oauthlib` validates returned state; Google recommends state validation for CSRF protection: <https://developers.google.com/identity/protocols/oauth2/resources/best-practices>;
- tokens, state, profile files, temporary content, and vault objects are made `0600`, with profile/state directories `0700` (`src/health_agent/google_drive/stores.py:16-46`; `src/health_agent/google_drive/vault_consumer.py:19-51`);
- no committed credential value was found by static pattern inspection, and default secret/data locations are ignored by Git.

However, `run_local_server` is called without `host`, so pinned library behavior uses `host="localhost"`, not the management requirement “listen only on `127.0.0.1`” (`src/health_agent/google_drive/oauth.py:35-40`; `tests/google_drive/test_oauth.py:94-99`). Google accepts localhost or literal loopback for Desktop clients, but that does not satisfy the product’s stricter bind requirement: <https://developers.google.com/identity/protocols/oauth2/resources/loopback-migration>. Pass `host="127.0.0.1"` (and preferably a finite timeout) and assert it.

The exact Drive read-only scope is account-wide, not folder-scoped. Full traversal limits downloads to configured roots, but the user Changes feed necessarily receives metadata changes outside those roots before filtering. The operator guide should disclose that OAuth grants read access to all files the account can access even though application logic only ingests configured roots. Google describes `drive.readonly` as viewing and downloading all Drive files: <https://developers.google.com/workspace/drive/api/guides/api-specific-auth>.

### 9. “Authorize once while the app is in Testing” is unsupported

The setup guide tells the operator to leave the personal OAuth app in Testing, add test users, and authorize each profile once (`docs/integrations/google-drive.md:22-24`; implementation report lines 23-25). Google states that an External/Testing consent screen issues refresh tokens that expire after seven days unless only basic identity scopes are requested; Drive read-only is not such a scope: <https://developers.google.com/identity/protocols/oauth2#expiration>.

Also, an expired/revoked refresh token simply escapes `credentials.refresh(...)` (`src/health_agent/google_drive/oauth.py:41-46`) instead of falling back to an explicit reauthorization-required status. Document the seven-day Testing limitation and either publish/configure the app appropriately for durable use or make reauthorization a first-class safe state.

### 10. CLI status overstates health

`authorized=yes` checks only whether `token.json` exists (`src/health_agent/cli.py:168-185`); it does not parse the token, validate exact scopes, refresh it, verify the account, or check root access. `cursor=ready` says nothing about last successful sync, lag, an interrupted scan, or per-file attention. `files=N` counts only items whose local state string is exactly `imported` (`src/health_agent/google_drive/stores.py:177-179`). Together with finding 1, a user can see a healthy-looking status while no medical record exists.

Expose distinct configured/auth-valid/account-bound/root-accessible/last-success/error/action-required states and medical outcomes. Avoid remote calls for a cheap local status if desired, but name that result `token_present`, not `authorized`.

### 11. Retry coverage is narrower than the claim

The gateway correctly retries 429, 5xx, and known rate-limit 403 reasons with bounded randomized exponential backoff, and does not retry 401 or ordinary permission 403 (`src/health_agent/google_drive/api.py:33-72`). This aligns with Google’s quota guidance: <https://developers.google.com/workspace/drive/api/guides/limits>.

It does not retry transport-level transient failures (timeouts, resets, `httplib2.HttpLib2Error`) because `_is_retryable` accepts only `HttpError`. More importantly, retries exhausted for one file become the whole-run failure described in finding 4. Narrow the report claim or add bounded transport retries plus per-item failure isolation.

## Test adequacy

The mocked service tests do exercise recursive traversal, list/change pagination at the service protocol, pre-scan token ordering, cursor advancement only after success, basic idempotency, removals, capability restrictions, and local profile separation. The content consumer test proves streaming hash/size/vault mode for synthetic chunks.

Material gaps remain:

- no mocked `GoogleDriveGateway` request/response test for `files.list`, `changes.list`, shared-drive change parsing, `get_media`/`export_media`, or a download larger than one chunk (`tests/google_drive/test_api.py` tests only retry classification/execution);
- OAuth test replaces the entire installed-app flow, so it does not prove callback bind, state mismatch rejection, PKCE, refresh failure, or account/token commit ordering (`tests/google_drive/test_oauth.py:66-100`);
- CLI tests cover only configure and local status; no `auth`, `sync`, `--full`, failure, or truthful medical-import assertion (`tests/google_drive/test_cli.py`);
- no tests for roots changed after cursor creation, unchanged-file path refresh, trash/untrash, shared-drive logs/drive changes, oversized export, per-item continuation, or automated safety reconciliation;
- no database/import/review/dashboard integration test and no real two-profile ownership test.

These gaps explain why a green mocked suite does not establish the implementation report’s broad “incremental sync,” “missing/removal reconciliation,” or v1 readiness claims.

## Verified design strengths

- Folder IDs and common HTTPS folder links are normalized, duplicate configured roots are removed, and unsafe profile path keys are rejected (`src/health_agent/google_drive/config.py`).
- Full inventory is breadth-first, recursive, and service-level pagination is complete (`src/health_agent/google_drive/service.py:149-185`).
- Taking the Changes token before inventory and committing it only after successful inventory avoids the scan race; incremental `newStartPageToken` is committed only after the terminal page and successful processing (`src/health_agent/google_drive/service.py:131-147,187-235`). Google confirms `nextPageToken`/terminal `newStartPageToken` semantics: <https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list>.
- PDF/image MIME selection is conservative; Docs, Sheets, Slides, and Drawings all officially support PDF export: <https://developers.google.com/workspace/drive/api/guides/ref-export-formats>.
- `MediaIoBaseDownload` is used with a 1 MiB buffer, yielded incrementally, and the consumer computes SHA-256/size while writing a temporary file before content-addressed storage (`src/health_agent/google_drive/api.py:178-199`; `src/health_agent/google_drive/vault_consumer.py:31-51`). Binary Drive-reported size is checked after completion (`src/health_agent/google_drive/service.py:288-296`).
- Reprocessing an identical Drive ID/revision is suppressed within a local profile, and vault/state directories are profile-specific. This is a good adapter boundary, provided finding 1 and the stable database ownership mapping are completed.

## Minimum merge gates

1. Wire a profile-aware Drive consumer into the real medical importer/database and make CLI counters/statuses reflect medical outcomes.
2. Choose and enforce shared-drive policy: implement per-log cursors and drive-change parsing, or reject such roots.
3. Force/reconcile full inventory when root configuration changes and refresh path provenance even for unchanged content.
4. Persist per-item safe failures/statuses, continue after isolated file failures, and handle trash explicitly.
5. Make OAuth account binding/token commit atomic; bind callback to `127.0.0.1`; represent revoked/Testing-expired tokens honestly.
6. Add the missing mocked gateway/OAuth/CLI edge tests plus database-backed two-profile and Drive-file-to-review/dashboard acceptance tests.
7. Perform a live acceptance with the user’s real private folder only after the above; do not claim it from mocked tests.
