# Telegram Connector Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the local Telegram foundation safe for unattended multi-profile use under ambiguous delivery, retries, crashes, token rotation, malformed API data, and untrusted attachments.

**Architecture:** A verified credential atomically binds the token file to a Bot API bot ID, and every durable identity/update/offset/outbound row is namespaced by that bot ID. Update processing uses owner/generation fenced claims with renewable leases and persisted bounded retry times. Attachments are staged, bounded, hashed, and magic-validated before the idempotent inbox is invoked; outbound mutation failures distinguish definite rejection/defer from unknown delivery.

**Tech Stack:** Python 3.13, stdlib SQLite/threading/tempfile, HTTPX, Typer, pytest.

## Global Constraints

- Continue using local long polling; never add or delete a webhook automatically.
- Never retry `sendMessage` after transport or 5xx ambiguity.
- Keep all state/profile ownership bot-scoped and preserve old bot namespaces.
- Persist no message body, reply body, attachment bytes, filename, caption, token, or medical text in audit tables.
- Reject symlinks/non-regular private paths before any file or SQLite operation.
- Use only mocked Telegram/downstream services and disposable PostgreSQL in tests.
- Add no Alembic revision; migrate the connector's replaceable local SQLite schema idempotently.

---

### Task 1: Bot-scoped private state and transactional identity management

**Files:**
- Modify: `src/health_agent/telegram/types.py`
- Modify: `src/health_agent/telegram/stores.py`
- Modify: `src/health_agent/telegram/admin.py`
- Test: `tests/telegram/test_stores.py`
- Test: `tests/telegram/test_admin.py`

**Interfaces:**
- Produces `VerifiedBotCredential`, bot-scoped runtime/identity/update/outbound methods, and atomic `bind_identity(...) -> TelegramIdentity`.
- Migrates legacy SQLite rows into bot ID `0`; verified bots always use their numeric namespace.

- [ ] Test and implement atomic verified credential storage plus bot namespace preservation across replacement.
- [ ] Test and implement bot/profile-scoped outbound keys with text digest/chat conflict detection.
- [ ] Test and implement one-transaction identity conflict checks and committed-result return.
- [ ] Test and implement rejection of symlink/non-regular token, directory, and SQLite paths.

### Task 2: Fenced claims and durable retry scheduling

**Files:**
- Modify: `src/health_agent/telegram/types.py`
- Modify: `src/health_agent/telegram/stores.py`
- Modify: `src/health_agent/telegram/service.py`
- Test: `tests/telegram/test_stores.py`
- Test: `tests/telegram/test_service.py`

**Interfaces:**
- Produces `UpdateClaim(owner_id, generation, lease_until, attempt_count)`, CAS renew/complete/defer methods, and `PollReport.blocked_until`.

- [ ] Test claim contention, renewal beyond the original lease, stale completion rejection, and generation increment after genuine expiry.
- [ ] Implement a processing heartbeat that renews the active claim and fences every final state transition.
- [ ] Persist exponential `next_retry_at`, cap autonomous attempts, sleep until due, and surface terminal `needs_attention` after exhaustion.

### Task 3: Mutation-aware Bot API and truthful status

**Files:**
- Modify: `src/health_agent/telegram/api.py`
- Modify: `src/health_agent/telegram/messenger.py`
- Modify: `src/health_agent/telegram/admin.py`
- Modify: `src/health_agent/cli.py`
- Test: `tests/telegram/test_api.py`
- Test: `tests/telegram/test_messenger.py`
- Test: `tests/telegram/test_admin_cli.py`

**Interfaces:**
- Produces typed `TelegramDeferred(retry_at)` and `TelegramDeliveryUnknown`; `sendMessage` never internally retries ambiguous failure.
- Produces verified status with bot ID, credential validity, webhook state, poll heartbeat freshness, and safe error code.

- [ ] Test read retry versus mutation no-retry, full `retry_after` deferral, and safe malformed response handling.
- [ ] Verify `getMe` before atomically saving bot ID/token and scope every runtime operation to that ID.
- [ ] Make status call `getMe` and `getWebhookInfo`, report configured/verified/webhook/running separately, and never expose token/API descriptions.

### Task 4: Pre-commit attachment staging and malformed-update quarantine

**Files:**
- Modify: `src/health_agent/telegram/service.py`
- Modify: `src/health_agent/telegram/types.py`
- Test: `tests/telegram/test_service.py`

**Interfaces:**
- Produces private staged attachments with independently verified size/SHA-256/media signature before `MedicalInbox.ingest`.

- [ ] Test PDF/JPEG/OGG signatures, lying MIME/size, over-limit streams, incomplete/incorrect inbox receipts, and no inbox side effect before validation.
- [ ] Test malformed update/API integer objects become persisted safe service errors and do not terminate `run_forever`.
- [ ] Implement private temporary staging/deletion and profile/source idempotency requirements at the inbox boundary.

### Task 5: Documentation, migration compatibility, and gates

**Files:**
- Modify: `docs/integrations/telegram.md`
- Modify: `docs/superpowers/reports/2026-09-04-telegram-connector-report.md`
- Create: `docs/superpowers/reports/2026-09-04-telegram-connector-hardening-report.md`

- [ ] Test opening an existing legacy connector SQLite file performs a lossless bot-0 migration and remains idempotent.
- [ ] Update setup/status/delivery/retry/staging documentation without claiming live acceptance.
- [ ] Run full pytest, Ruff, mypy, disposable migrations, diff/credential checks; commit all hardening changes and do not push.
