# Google Drive Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-profile, read-only Google Drive connector that recursively and incrementally streams medical files into a local consumer without changing the current database schema.

**Architecture:** `DriveService` owns profile-safe orchestration; an official Google API gateway owns OAuth and Drive HTTP calls; small `ProfileStore`, `SyncStateStore`, and `ContentConsumer` protocols isolate configuration, cursor state, and future database/import integration. Local JSON implementations keep each profile in a separate `0600` directory, while the current vault adapter receives streamed bytes without loading whole files or logging content.

**Tech Stack:** Python 3.13, Typer, Google API Python client, google-auth-oauthlib, tenacity, pytest.

## Global Constraints

- OAuth scope is exactly `https://www.googleapis.com/auth/drive.readonly`.
- Drive is never written to, moved, deleted, renamed, or shared by this connector.
- Profile identifiers are present at every configuration, token, state, and content boundary.
- No credentials, medical content, or base64 content enter logs or Git.
- No database migration is introduced in this branch.

---

### Task 1: Profile configuration and local stores

**Files:** Create `src/health_agent/google_drive/config.py`, `src/health_agent/google_drive/stores.py`; test in `tests/google_drive/test_config.py` and `tests/google_drive/test_stores.py`.

- [x] Parse and normalize raw folder IDs and supported Drive folder URLs.
- [x] Persist profile config, OAuth token, cursor, and seen revisions in profile-isolated files with mode `0600`.
- [x] Prove invalid profile IDs, cross-profile keys, malformed URLs, and permissive file modes are rejected or corrected.
- [x] Run focused tests and commit with the complete connector.

### Task 2: Official API gateway and recursive/incremental sync

**Files:** Create `src/health_agent/google_drive/api.py`, `src/health_agent/google_drive/service.py`, `src/health_agent/google_drive/types.py`; test in `tests/google_drive/test_service.py` and `tests/google_drive/test_api.py`.

- [x] Define gateway/store/consumer protocols and immutable provenance records.
- [x] Implement paginated recursive traversal, explicit shortcut skipping, supported binary MIME handling, safe Google-native PDF export, and download capability checks.
- [x] Stream chunks into the injected consumer and record size/SHA-256 returned by it.
- [x] Save a pre-scan Changes token after a successful full scan and advance `newStartPageToken` only after successful incremental processing.
- [x] Retry transient API failures with bounded exponential backoff and never retry authorization or access failures.
- [x] Prove repeated scans are idempotent and profile state never crosses boundaries.

### Task 3: OAuth, CLI, and operator handoff

**Files:** Create `src/health_agent/google_drive/oauth.py`, `src/health_agent/google_drive/vault_consumer.py`; modify `src/health_agent/cli.py`, `src/health_agent/config.py`, `README.md`, `.env.example`, `pyproject.toml`; test in `tests/google_drive/test_oauth.py` and `tests/google_drive/test_cli.py`.

- [x] Add `drive configure`, `drive auth`, `drive status`, and `drive sync` commands backed by the same service functions a localhost panel can call later.
- [x] Use Desktop OAuth with a loopback callback and per-profile token file.
- [x] Document the single required Google Cloud OAuth client setup, folder requirements, and the database/provenance adapter still owned by the concurrent profile migration.
- [x] Run `uv run pytest -q`, `uv run ruff check .`, and `uv run mypy src`; fix all failures and commit.
