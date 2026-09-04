# Gmail Connector Review Fixes Round 2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the remaining Gmail re-review gaps without retaining or over-classifying arbitrary message bodies.

**Architecture:** Recognized body-only medical messages create an idempotent minimal `SourceRecord` in PostgreSQL and a safe local attention record; raw bodies remain ephemeral. Full and recovery sync reconcile every previously active Gmail message by ID before publishing the new cursor. Attachment records retain outcome plus processing reason so OCR reporting is exact.

**Tech Stack:** Python 3.13, Typer, SQLAlchemy/PostgreSQL, Gmail API, pytest.

## Global Constraints

- Gmail access remains exactly `gmail.readonly`.
- No message body, token, filename, sender, or exception message is printed by the operational CLI.
- Body classification is conservative and queues recognized medical content; it does not claim clinical interpretation.
- No live Google credentials or user data are used.

---

### Task 1: Common body-message inbox

**Files:** `gmail/classifier.py`, `gmail/message_inbox.py`, `gmail/types.py`, `gmail/service.py`, `gmail/stores.py`, `cli.py`, and focused tests.

- [x] Recognize conservative appointment and other medical body signals from a bounded transient prefix.
- [x] Insert idempotent, profile-scoped, content-free Gmail body provenance into `source_records`.
- [x] Persist and list safe message attention identifiers/classification without body text.

### Task 2: Full/recovery reconciliation

**Files:** `gmail/service.py`, `gmail/stores.py`, and service/store tests.

- [x] Enumerate previously relevant message IDs under the existing account lock.
- [x] Fetch and reconcile current deletion/Spam/Trash/restored state before the full/recovery cursor commit.
- [x] Cover manual full and expired-history recovery regressions.

### Task 3: Exact OCR and unnamed attachments

**Files:** `gmail/classifier.py`, `gmail/service.py`, `gmail/types.py`, `gmail/medical_importer.py`, `cli.py`, and tests.

- [x] Preserve processing reason and report image/scanned OCR as `ocr_required` in run, lifetime status, and attention listing.
- [x] Accept supported unnamed attachment parts and assign deterministic safe synthetic filenames.
- [x] Add CLI-level image OCR and unnamed-PDF regressions.

### Task 4: OAuth preflight freshness and final verification

**Files:** `gmail/stores.py`, `cli.py`, docs/report, and tests.

- [x] Record every sync preflight attempt and update every failure timestamp without changing last success.
- [x] Reconcile documentation and append the implementation report.
- [x] Run Gmail and full tests, Ruff, mypy, credential scan, CLI checks, and migration up/down/up; commit without pushing.
