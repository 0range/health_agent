# Google Sheets v0.1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every health profile one safe, idempotently synchronized Google spreadsheet for verified lab history, uncertain lab decisions, and connector freshness.

**Architecture:** PostgreSQL remains authoritative. A profile-bound Google Sheets connector validates a dedicated OAuth identity and a hidden remote workbook binding, imports a strictly validated batch of review decisions transactionally, then atomically replaces its managed projections through a narrow gateway. Private local files hold connector credentials/configuration; PostgreSQL holds medical decision and sync audit records.

**Tech Stack:** Python 3.13, Typer, SQLAlchemy 2, PostgreSQL 18, Alembic, Google Sheets API v4, Google Drive API v3, google-auth-oauthlib, pytest, Ruff, mypy.

## Global Constraints

- One spreadsheet per profile with exactly the managed tabs `Lab history`, `Needs review`, `Sources`, and hidden `_HealthAgent`.
- PostgreSQL is the source of truth; only decision and correction cells in `Needs review` are imported.
- Every database query and connector file is profile-scoped; a verified Google account cannot cross profile boundaries.
- OAuth grants exactly `https://www.googleapis.com/auth/spreadsheets` and `https://www.googleapis.com/auth/drive.file`; Drive read-only credentials are never promoted to write credentials.
- Managed Sheets data excludes document bodies, evidence excerpts, vault paths, provider payloads, email bodies, attachment names, tokens, and credentials.
- Invalid schema, identity, ownership, duplicate IDs, stale row versions, malformed correction, or conflicting replay fails closed before any decision is applied.
- Sync is locally locked, repeated projection is idempotent, decision application is transactional, and remote managed-sheet replacement is one atomic Google `batchUpdate`.
- No implementation or test performs real OAuth, Google network requests, or reads production medical data.
- CLI failures print stable safe error codes and counts only, never medical cell contents or remote error bodies.

---

### Task 1: Profile-bound configuration, credentials, and Google gateway

**Files:**
- Create: `src/health_agent/google_sheets/__init__.py`
- Create: `src/health_agent/google_sheets/types.py`
- Create: `src/health_agent/google_sheets/config.py`
- Create: `src/health_agent/google_sheets/stores.py`
- Create: `src/health_agent/google_sheets/oauth.py`
- Create: `src/health_agent/google_sheets/api.py`
- Modify: `src/health_agent/config.py`
- Test: `tests/google_sheets/test_config.py`
- Test: `tests/google_sheets/test_stores.py`
- Test: `tests/google_sheets/test_oauth.py`
- Test: `tests/google_sheets/test_api.py`

**Interfaces:**
- Produces: `SheetsProfile`, `SheetsAccountIdentity`, `WorkbookBinding`, `LocalSheetsProfileStore`, `LocalSheetsTokenStore`, `LocalSheetsStateStore`, `SheetsOAuth`, `GoogleSheetsGateway`, and the `SheetsGateway` protocol.
- Consumes: `health_agent.google_drive.config.validate_profile_id`, the Drive profile/token stores for optional expected-account reuse, and existing private atomic-file conventions.

- [ ] **Step 1: Write failing ownership/configuration and private-file tests**

```python
def test_profile_requires_uuid_and_preserves_expected_drive_identity(tmp_path):
    profile = SheetsProfile.create(PROFILE_ID, expected_permission_id="perm-1", expected_email="me@example.com")
    store = LocalSheetsProfileStore(tmp_path)
    store.save(profile)
    assert store.load(PROFILE_ID) == profile
    assert stat.S_IMODE((tmp_path / PROFILE_ID / "profile.json").stat().st_mode) == 0o600

def test_token_store_rejects_same_google_account_for_another_profile(tmp_path):
    store = LocalSheetsTokenStore(tmp_path)
    store.publish_verified(PROFILE_ID, SheetsAccountIdentity("perm-1", "me@example.com"), VALID_TOKEN_JSON)
    with pytest.raises(ValueError, match="another health profile"):
        store.publish_verified(OTHER_PROFILE_ID, SheetsAccountIdentity("perm-1", "me@example.com"), VALID_TOKEN_JSON)
```

- [ ] **Step 2: Run Task 1 configuration/store tests and verify missing-module failures**

Run: `uv run pytest tests/google_sheets/test_config.py tests/google_sheets/test_stores.py -q`

Expected: FAIL during collection because `health_agent.google_sheets` does not exist.

- [ ] **Step 3: Implement exact-scope configuration and symlink-safe atomic stores**

```python
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
SHEETS_SCOPES = frozenset((SHEETS_SCOPE, DRIVE_FILE_SCOPE))

@dataclass(frozen=True, slots=True)
class SheetsProfile:
    profile_id: str
    expected_permission_id: str | None
    expected_email: str | None
    spreadsheet_id: str | None = None
    spreadsheet_url: str | None = None
    workbook_token: str | None = None
```

Use `lstat`, `O_NOFOLLOW`, `fcntl.flock`, temporary sibling files, `os.replace`, mode `0700` directories, and mode `0600` JSON/token/lock files. Validate paired identities, opaque Google IDs, HTTPS spreadsheet URLs, persisted profile-directory ownership, and exact credential scope sets.

- [ ] **Step 4: Write failing OAuth identity/scope tests**

```python
def test_authorize_verifies_expected_account_before_publishing(oauth, gateway_factory, token_store):
    gateway_factory.identity = SheetsAccountIdentity("different", "other@example.com")
    with pytest.raises(SheetsAccountMismatch):
        oauth.authorize(PROFILE_ID, interactive=False)
    assert not token_store.exists(PROFILE_ID)

def test_load_rejects_any_extra_scope(token_store, oauth):
    token_store.write_fixture(PROFILE_ID, scopes=[*SHEETS_SCOPES, "https://www.googleapis.com/auth/drive"])
    with pytest.raises(SheetsOAuthScopeError):
        oauth.load(PROFILE_ID)
```

- [ ] **Step 5: Implement staged OAuth and verified publishing**

`SheetsOAuth.stage(profile_id, force=False, interactive=False)` loads or refreshes exact-scope credentials and never publishes unverified state. `authorize(...)` builds a gateway, reads `about.user(permissionId,emailAddress)`, compares the identity with the configured/reused Drive binding, atomically binds the Sheets profile and publishes credentials, and refuses a cross-profile token/account collision.

- [ ] **Step 6: Write failing gateway request-shape, redaction, retry, and atomic-batch tests**

```python
def test_replace_managed_tabs_uses_one_atomic_batch(service, gateway, workbook):
    gateway.replace_managed_tabs(workbook)
    assert len(service.spreadsheets().batchUpdate_calls) == 1
    body = service.spreadsheets().batchUpdate_calls[0]["body"]
    assert {request_key(r) for r in body["requests"]} >= {"updateCells", "setDataValidation"}

def test_safe_error_never_contains_google_payload():
    error = fake_http_error(403, b'{"error":{"message":"private remote text"}}')
    assert "private remote text" not in safe_sheets_error_code(error)
```

- [ ] **Step 7: Implement the mockable Sheets/Drive API gateway**

The gateway exposes `account_identity()`, `create_workbook(title, binding)`, `read_binding(spreadsheet_id)`, `read_review_rows(spreadsheet_id)`, and `replace_managed_tabs(spreadsheet_id, projection)`. Use bounded authorized HTTP, the repository retry rules for 429/5xx/transient errors, field masks, and one `spreadsheets.batchUpdate` for all managed writes/formatting.

- [ ] **Step 8: Run Task 1 tests, Ruff, mypy, and commit**

Run:

```bash
uv run pytest tests/google_sheets/test_config.py tests/google_sheets/test_stores.py tests/google_sheets/test_oauth.py tests/google_sheets/test_api.py -q
uv run ruff check src/health_agent/google_sheets src/health_agent/config.py tests/google_sheets
uv run mypy src/health_agent/google_sheets src/health_agent/config.py
git add src/health_agent/google_sheets src/health_agent/config.py tests/google_sheets
git commit -m "feat: add profile-bound Google Sheets connector"
```

Expected: all commands PASS.

---

### Task 2: Database audit, safe projections, and review decision import

**Files:**
- Create: `src/health_agent/google_sheets/models.py`
- Create: `src/health_agent/google_sheets/projection.py`
- Create: `src/health_agent/google_sheets/decisions.py`
- Create: `alembic/versions/0006_google_sheets.py`
- Modify: `tests/conftest.py`
- Test: `tests/google_sheets/test_projection.py`
- Test: `tests/google_sheets/test_decisions.py`
- Test: `tests/google_sheets/test_schema.py`

**Interfaces:**
- Consumes: `SheetsProjection`, `ReviewSheetRow`, `WorkbookBinding` from Task 1; existing `approve_observation`, `correct_observation`, and `reject_observation` functions.
- Produces: `build_projection(session, profile_id, source_statuses) -> SheetsProjection`, `parse_decisions(rows, expected_rows, profile_id) -> tuple[ReviewDecision, ...]`, and `apply_decisions(session, profile_id, spreadsheet_id, decisions) -> DecisionReport`.

- [ ] **Step 1: Write failing migration and audit-constraint tests**

```python
def test_decision_audit_identity_is_unique(session, pending_review):
    first = decision_audit(pending_review, hash="same")
    session.add(first)
    session.flush()
    session.add(decision_audit(pending_review, hash="same"))
    with pytest.raises(IntegrityError):
        session.flush()

def test_migration_roundtrip_preserves_previous_head(disposable_postgres):
    downgrade("0005_whoop")
    upgrade("head")
    assert {"sheets_sync_runs", "sheets_review_decision_audits"} <= table_names()
```

- [ ] **Step 2: Add profile-scoped sync and immutable decision-audit models/migration**

`SheetsSyncRun` records status, safe error code, counts, and timestamps. `SheetsReviewDecisionAudit` records profile/review item/observation, spreadsheet ID and row number, row version, action, decision hash, correction JSON, and applied time. Add explicit foreign keys, bounded status/action checks, UUID/indexes, and a unique decision identity.

- [ ] **Step 3: Write failing projection privacy and multi-profile tests**

```python
def test_projection_contains_only_verified_rows_for_requested_profile(session):
    projection = build_projection(session, PROFILE_ID, ())
    assert {r.observation_id for r in projection.lab_history} == {OWN_VERIFIED_ID}
    assert OTHER_PROFILE_VALUE not in repr(projection)

def test_projection_never_exports_raw_medical_or_secret_fields(session):
    projection = build_projection(session, PROFILE_ID, ())
    rendered = repr(projection)
    assert "evidence_excerpt" not in rendered
    assert "vault_path" not in rendered
    assert "oauth-token" not in rendered
```

- [ ] **Step 4: Implement deterministic lab, review, and source projections**

Medical date is `collected_date` then `issued_date`, never `created_at`. Choose the oldest source URI for a deterministic link without exporting its external filename. Review row version is SHA-256 over canonical JSON containing profile ID, review item ID, observation ID, immutable displayed evidence, reason, and status. Sort lab rows by medical date then observation UUID; sort review rows by creation then UUID; sort sources by source/account.

- [ ] **Step 5: Write failing review-parser and transactional-decision tests**

```python
@pytest.mark.parametrize("mutation", ["unknown_id", "duplicate_id", "wrong_profile", "stale_version", "bad_decision", "correction_on_approve"])
def test_invalid_review_grid_applies_nothing(session, review_grid, mutation):
    rows = mutate(review_grid, mutation)
    with pytest.raises(ReviewGridError):
        parse_decisions(rows, expected_rows(session, PROFILE_ID), PROFILE_ID)
    assert pending_status(session) == ReviewStatus.NEEDS_REVIEW

def test_valid_mixed_batch_is_all_or_nothing(session, three_pending_rows):
    decisions = (approve(...), correct(...), reject(...))
    report = apply_decisions(session, PROFILE_ID, SPREADSHEET_ID, decisions)
    assert report == DecisionReport(approved=1, corrected=1, rejected=1, replayed=0)
    assert audit_actions(session) == ["approve", "correct", "reject"]
```

- [ ] **Step 6: Implement strict parsing, replay rules, and transactional application**

Validate the entire rectangular grid before returning any decisions. Blank decision is ignored. `correct` requires nonblank corrected value and rejects unsupported normalization. Identical audited replay is counted without changing medical rows; a different action/hash for the same row version raises `ReviewConflict`. Apply all new decisions under one nested/session transaction through the existing importer functions, then append audit rows without medical excerpts.

- [ ] **Step 7: Run Task 2 tests and commit**

Run:

```bash
uv run pytest tests/google_sheets/test_schema.py tests/google_sheets/test_projection.py tests/google_sheets/test_decisions.py -q
uv run ruff check src/health_agent/google_sheets alembic/versions/0006_google_sheets.py tests/google_sheets tests/conftest.py
uv run mypy src/health_agent/google_sheets
git add src/health_agent/google_sheets alembic/versions/0006_google_sheets.py tests/google_sheets tests/conftest.py
git commit -m "feat: project labs and import audited Sheets decisions"
```

Expected: all commands PASS.

---

### Task 3: Idempotent sync orchestration and convergence

**Files:**
- Create: `src/health_agent/google_sheets/service.py`
- Create: `src/health_agent/google_sheets/sources.py`
- Test: `tests/google_sheets/test_service.py`
- Test: `tests/google_sheets/test_sources.py`

**Interfaces:**
- Consumes: gateway/config/store interfaces from Task 1 and projection/decision interfaces from Task 2.
- Produces: `SheetsService.configure(profile_id)`, `SheetsService.authorize(profile_id, force, interactive)`, `SheetsService.sync(profile_id) -> SheetsSyncReport`, and `SheetsService.status(profile_id) -> SheetsStatus`.

- [ ] **Step 1: Write failing first-sync, stable-resync, and ownership tests**

```python
def test_first_sync_creates_and_binds_one_workbook(service, gateway):
    report = service.sync(PROFILE_ID)
    assert report.status == "succeeded"
    assert gateway.created == 1
    assert service.status(PROFILE_ID).spreadsheet_configured is True

def test_repeat_sync_does_not_create_another_workbook(service, gateway):
    service.sync(PROFILE_ID)
    service.sync(PROFILE_ID)
    assert gateway.created == 1

def test_wrong_hidden_profile_aborts_before_reading_or_writing(service, gateway):
    gateway.binding = WorkbookBinding(OTHER_PROFILE_ID, SCHEMA_VERSION, TOKEN)
    with pytest.raises(WorkbookOwnershipError):
        service.sync(PROFILE_ID)
    assert gateway.review_reads == 0
    assert gateway.writes == 0
```

- [ ] **Step 2: Write failing convergence and all-or-nothing review tests**

```python
def test_remote_write_failure_keeps_local_decision_and_next_run_converges(service, gateway, decision_row, session):
    gateway.fail_next_write = True
    with pytest.raises(SheetsRemoteError):
        service.sync(PROFILE_ID)
    assert observation_status(session, decision_row.observation_id) == ReviewStatus.VERIFIED
    assert last_sync_status(session) == "failed"
    assert service.sync(PROFILE_ID).status == "succeeded"
    assert decision_row.observation_id not in gateway.latest_review_ids

def test_one_malformed_row_rolls_back_entire_decision_batch(service, gateway, two_rows):
    gateway.review_rows = (valid_decision(two_rows[0]), malformed_decision(two_rows[1]))
    with pytest.raises(ReviewGridError):
        service.sync(PROFILE_ID)
    assert both_pending(two_rows)
```

- [ ] **Step 3: Implement locked orchestration and safe run recording**

Verify profile existence, configuration, exact OAuth state, live identity, and workbook binding before review reads. For a new workbook, create/bind it only after identity validation. Parse the whole decision grid, commit it transactionally with an audit, rebuild projection from a fresh transaction, then replace managed tabs. Record every attempt as `started`, then `succeeded` or `failed` with only a stable mapped code. Never swallow `KeyboardInterrupt`/`SystemExit`.

- [ ] **Step 4: Write failing source-status isolation/freshness tests**

```python
def test_source_statuses_include_only_requested_profile(settings, session):
    rows = collect_source_statuses(settings, session, PROFILE_ID, NOW)
    assert {(r.source, r.account) for r in rows} == {("whoop", "main"), ("drive", "main"), ("gmail", "lab")}
    assert OTHER_PROFILE_ID not in repr(rows)

def test_source_status_never_contains_token_or_provider_payload(source_files):
    assert "secret-token" not in repr(collect_source_statuses(...))
```

- [ ] **Step 5: Implement safe Drive, Gmail, WHOOP, and Sheets freshness adapters**

Read only local connector status methods and WHOOP connection run metadata. Emit stable authorization/freshness labels and safe error codes. Missing/unconfigured connectors are represented without becoming sync failures. Do not open token JSON directly outside existing store/OAuth status methods and do not expose emails as account labels.

- [ ] **Step 6: Run Task 3 tests and commit**

Run:

```bash
uv run pytest tests/google_sheets/test_service.py tests/google_sheets/test_sources.py -q
uv run ruff check src/health_agent/google_sheets tests/google_sheets
uv run mypy src/health_agent/google_sheets
git add src/health_agent/google_sheets tests/google_sheets
git commit -m "feat: synchronize profile-scoped health spreadsheets"
```

Expected: all commands PASS.

---

### Task 4: CLI, automation readiness, documentation, and whole-slice gates

**Files:**
- Modify: `src/health_agent/cli.py`
- Modify: `src/health_agent/automation/models.py`
- Modify: `src/health_agent/automation/registry.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/google-sheets.md`
- Test: `tests/google_sheets/test_cli.py`
- Modify: `tests/automation/test_registry.py`
- Modify: `tests/automation/test_runner.py`
- Create: `tests/google_sheets/test_integration.py`

**Interfaces:**
- Consumes: `SheetsService` and configuration types from Tasks 1–3.
- Produces: the `sheets configure`, `sheets authorize`, `sheets sync`, and `sheets status` CLI; a deferred-aware `sheets` automation source/job.

- [ ] **Step 1: Write failing CLI registration, composition, and redaction tests**

```python
def test_sheets_commands_are_registered():
    result = CliRunner().invoke(cli.app, ["sheets", "--help"])
    assert result.exit_code == 0
    assert all(name in result.stdout for name in ("configure", "authorize", "sync", "status"))

def test_sync_failure_prints_only_safe_code(monkeypatch):
    monkeypatch.setattr(cli, "_build_sheets_service", exploding_service("private lab value"))
    result = CliRunner().invoke(cli.app, ["sheets", "sync", PROFILE_ID])
    assert result.exit_code == 1
    assert result.stdout == "status=failed safe_error=sheets_sync_failed\n"
    assert "private lab value" not in result.output
```

- [ ] **Step 2: Implement CLI composition and safe output**

`configure` verifies the database profile and optionally reuses the verified Drive account binding; it does not authorize or create a spreadsheet. `authorize` is the only interactive command. `sync` is non-interactive. `status` never refreshes or performs a network request. Map typed connector errors to stable codes and never include exception text.

- [ ] **Step 3: Write failing automation discovery/defer/isolation tests**

```python
def test_registry_discovers_configured_sheets_profile(settings):
    jobs = SheetsJobAdapter().discover(settings)
    assert jobs == (AutomationJob("sheets", PROFILE_ID, "main", True, ("sheets", "sync", PROFILE_ID)),)

def test_missing_sheets_oauth_is_deferred_without_blocking_other_jobs(settings):
    jobs = all_jobs(settings)
    sheets = next(job for job in jobs if job.source == "sheets")
    assert sheets.deferred_reason == "oauth_not_ready"
    assert next(job for job in jobs if job.source == "whoop").deferred_reason is None
```

- [ ] **Step 4: Extend automation models/registry for Sheets**

Add `sheets` to the bounded source type and registry. Discover only safe UUID profile directories containing a valid `profile.json`; reject symlinks. A missing token produces `oauth_not_ready`, while malformed configuration is isolated by the existing runner behavior.

- [ ] **Step 5: Add a disposable-PostgreSQL/fake-Google integration test**

The test creates two profiles, verified and pending observations for both, configures one fake Google account, runs initial sync, submits approve/correct/reject rows, runs again, and asserts profile isolation, immutable audit provenance, exact verified history, empty handled queue, safe source statuses, one workbook, and an idempotent third run.

- [ ] **Step 6: Document local setup and non-network test procedure**

Document the one-spreadsheet model, tabs and editable fields, Google Cloud APIs/scopes, shared Desktop OAuth client file, commands, local private paths, automation behavior, recovery from safe error codes, profile/account isolation, and explicitly state that Drive remains read-only and PostgreSQL authoritative. Add `GOOGLE_SHEETS_ROOT`, `GOOGLE_SHEETS_CLIENT_SECRETS`, and timeout settings to `.env.example` without values resembling secrets.

- [ ] **Step 7: Run focused and full release gates**

Run:

```bash
uv run pytest tests/google_sheets tests/automation/test_registry.py tests/automation/test_runner.py -q
uv run pytest -q
uv run ruff check .
uv run mypy .
uv lock --check
git diff --check
```

Run Alembic against a disposable database and verify both `upgrade head`, `downgrade 0005_whoop`, and a second `upgrade head` succeed. Expected: every gate PASS and no production database, OAuth file, or Google API is touched.

- [ ] **Step 8: Commit the completed vertical slice**

```bash
git add src/health_agent/cli.py src/health_agent/automation .env.example README.md docs/google-sheets.md tests/google_sheets tests/automation
git commit -m "feat: expose Google Sheets sync and automation"
```

Expected: clean worktree whose commits contain design, plan, implementation, tests, migration, and documentation.
