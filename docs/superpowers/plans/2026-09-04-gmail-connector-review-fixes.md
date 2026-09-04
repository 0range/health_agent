# Gmail Connector Review Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the mocked Gmail foundation into a truthful Gmail-to-medical-record connector and close every finding in `gmail-connector-review.md`.

**Architecture:** Keep Gmail API/OAuth, mailbox orchestration, safe attachment preparation, and PostgreSQL medical import as separate units. Serialize each profile/account sync with a cross-process file lock, stage and validate bytes before any importer side effect, and use the existing profile-aware `import_document` transaction as the PDF system of record.

**Tech Stack:** Python 3.13, Typer, SQLAlchemy/PostgreSQL, Google Gmail API/OAuth, PyMuPDF, `fcntl`, pytest.

## Global Constraints

- Gmail access remains exactly `gmail.readonly`; no mailbox mutation is added.
- No medical content, bytes, token, or exception message is logged.
- Delivery is explicitly at-least-once; stable provenance and the common importer make repeats idempotent.
- No live Google credentials or user files are used in tests.

---

### Task 1: Transactional state and truthful status

**Files:** `src/health_agent/gmail/types.py`, `stores.py`, `service.py`, and store/service tests.

- [x] Add a cross-process account lock, safe symlink rejection, immutable attachment revision records, and durable run metadata.
- [x] Hold the lock across the complete sync and commit the cursor only after item records are durable.
- [x] Add overlapping-sync, status/failure, and revision-history regression tests.

### Task 2: Consistent mailbox and bounded transport semantics

**Files:** `src/health_agent/gmail/api.py`, `types.py`, `service.py`, and API/service tests.

- [x] Carry message labels and all relevant history changes; apply the same Spam/Trash policy to full and incremental runs, including restored mail.
- [x] Add repeated-page-token guards, explicit HTTP/OAuth callback timeouts, and bounded transient transport retry.
- [x] Add tests for Spam/Trash transitions, restored messages, request parameters, timeouts, transport retry, and pagination loops.

### Task 3: Safe autonomous content routing and real medical import

**Files:** `src/health_agent/gmail/medical_importer.py`, `classifier.py`, `service.py`, `cli.py`, and importer/classifier/CLI tests.

- [x] Include body-only appointment messages without retaining their content.
- [x] Incrementally decode to a private temporary file with pre-download and hard decoded-size bounds; validate size and magic/MIME before downstream effects.
- [x] Content-classify metadata-ambiguous PDFs locally; route medical PDFs through profile-aware `import_document`, retain provenance, and mark image/OCR uncertainty as attention rather than imported.
- [x] Report staged, medically imported, duplicate, OCR, and attention outcomes truthfully; prove at-least-once idempotency.

### Task 4: Safe OAuth publication and operational setup

**Files:** `src/health_agent/gmail/oauth.py`, `stores.py`, `cli.py`, configuration/docs, and OAuth/CLI tests.

- [x] Stage new credentials, verify the bound mailbox, reject cross-profile duplicate binding, then atomically publish credentials plus identity; preserve the old token on every failure.
- [x] Detect refresh failure as durable `oauth_required`; make status validate token format/scope and show freshness/failure history.
- [x] Document External/Testing seven-day expiry and require declared Production/Internal mode for unattended operation.

### Task 5: Migration regression and final gates

**Files:** `alembic/versions/0003_review_corrections.py`, migration regression tests, Gmail docs/report.

- [x] Drop/recreate the legacy verified view around the 0003 downgrade column removal and regression-test fresh up/down/up.
- [x] Reconcile README/runbook claims with real behavior and append the implementation report.
- [x] Run Gmail tests, full pytest, Ruff, mypy, credential scan, and disposable PostgreSQL up/down/up before committing.
