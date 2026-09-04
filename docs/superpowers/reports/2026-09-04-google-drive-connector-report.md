# Google Drive connector implementation report

## Result

- Branch: `codex/v1-google-drive`
- Scope: complete mocked/disposable-DB Drive slice; no real Google account or
  private medical file was used.

Implemented per-profile folder configuration, exact read-only OAuth, private
tokens/state, recursive/incremental sync, medical DB/review import, safe attention
states, and profile-isolated provenance. CLI commands are `drive configure`,
`drive auth`, `drive status`, and `drive sync`.

## Verification

- `uv run pytest -q`: 277 passed; only existing PyMuPDF SWIG deprecation warnings.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed.
- `git diff --check`: passed before commit.
- Credential-pattern scan found identifiers and documentation only, no credential values.

## Only external setup still required

Create one Google Cloud **Desktop OAuth client**, enable Drive API, save its JSON
to the ignored `data/secrets/google-drive-oauth-client.json`, then authorize each
profile. Testing-mode authorization may need repeating every seven days; see the
operator guide. Live acceptance was intentionally not attempted.

## Review fix — 2026-09-04

Rebased onto `codex/v1-slice-1` at `5b83869` and closed the full blocking review
set without touching a real Drive account or OAuth credential.

- `drive sync` now uses `MedicalDriveConsumer`: PDFs enter the shared
  PostgreSQL importer/review flow with immutable `google_drive` source
  occurrence/revision; images and invalid PDFs enter profile-scoped attention.
- The database Profile UUID is the fail-closed identifier at configuration,
  token, state, consumer, importer, and query boundaries. A disposable-Postgres
  test proves identical Drive IDs and bytes for two profiles remain separate.
- Shared-drive roots are explicitly rejected in v1. Drive membership changes
  are parsed without assuming `fileId`; My Drive keeps one profile cursor.
- Root changes clear the cursor, removed roots reconcile, and unchanged file
  content still refreshes root/ancestor/path provenance.
- Unsupported, shortcut, restricted, oversized export, corrupt, trash, removal,
  and isolated processing failures have safe per-item states; later valid files
  continue and the run cursor can progress.
- OAuth is staged without persistence, binds by stable Google `permissionId`,
  and publishes binding plus credential in one private atomic file only after
  identity validation. Failed lookup or mismatched reauthorization preserves
  the previous token. Callback bind is `127.0.0.1` with a five-minute timeout.
- Status reports local token validity, stable binding, root accessibility,
  interrupted sync, timestamps, errors, medical outcomes, and action count.
  Documentation now states account-wide scope and the seven-day External/Testing
  refresh-token caveat.
- Retries cover quota/server responses and transport failures; gateway tests
  cover request fields, change parsing, trash, and multi-chunk downloads.

Verification after the fix:

- `uv run pytest -q tests/google_drive`: 52 passed.
- `uv run pytest -q`: 277 passed.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed (41 source files).
- `uv run alembic upgrade head && uv run alembic check`: passed against a fresh,
  disposable PostgreSQL container; no migration was added.
- `git diff --check`: passed.
- `uv run health-agent drive --help`: passed.

Live OAuth, root access, download, and sync remain an explicit acceptance step
after the user is ready to authorize Google. They are not claimed by this report.

## Final review fix — 2026-09-04

Rebased onto `codex/v1-slice-1` at `0d65969` and closed the four blockers from
the final re-review without contacting Google or using real medical data.

- The importer conservatively extracts explicitly labelled collection and issue
  dates from PDF text. Collection date remains primary and issue date the
  fallback; Drive timestamps are never treated as medical dates. Review output
  exposes the dates and document ID, and `review set-date` supports correction.
- Retryable per-file outcomes are retained in durable profile state and replayed
  before the next ordinary incremental change scan, so cursor advancement does
  not lose an exhausted transient download or processing attempt.
- Root replacement and cursor invalidation now share the same per-profile lock
  as sync. The sync CLI loads the current roots only after acquiring that lock.
- Drive API operations and token refresh use the bounded
  `GOOGLE_DRIVE_HTTP_TIMEOUT_SECONDS` setting (30 seconds by default).
- CLI acceptance covers successful authorization, normal and `--full` sync,
  safe failure status, and the exact synthetic Drive PDF → date review → lab
  approval → shipped Metabase query path.

Final local verification:

- `uv run pytest -q tests/google_drive`: 59 passed.
- `uv run pytest -q`: 434 passed.
- `uv run ruff check .`: passed.
- `uv run mypy src`: passed (55 source files).
- `uv lock --check` and `git diff --check`: passed.
- `uv run alembic upgrade head && uv run alembic check`: passed against a fresh,
  disposable PostgreSQL container; no migration was required.

All Drive/OAuth tests in this result are mocked. A live private-folder smoke is
still an explicit user-authorized acceptance step and is not claimed here.
