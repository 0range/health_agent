# Gmail Medical Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a multi-profile, multi-account, read-only Gmail connector that autonomously routes likely medical PDF/image attachments into an injected importer.

**Architecture:** A Gmail application service is bound to exactly one profile/account pair. Official Google API and OAuth adapters provide only read calls; local `0600` config/token/state stores keep durable `historyId`, message decisions, and attachment revisions; importer and state protocols remain independent of concurrent database migrations.

**Tech Stack:** Python 3.13, Typer, Google API Python client, google-auth-oauthlib, tenacity, pytest.

## Global Constraints

- OAuth scope is exactly `https://www.googleapis.com/auth/gmail.readonly`.
- Default first scan is `newer_than:7d`; an expired `historyId` automatically falls back to that scan.
- No message body, attachment bytes, OAuth secret, or extracted medical text is logged.
- Profiles and Gmail accounts are mandatory at every config, token, state, and import boundary.
- No database migration is introduced in this branch.

---

### Task 1: Multi-account config, tokens, and state

- [x] Validate UUID profile IDs and safe account keys; store multiple bound emails per profile.
- [x] Persist account tokens, cursors, message decisions, and attachment provenance in private atomic JSON files.
- [x] Prove profile/account isolation and `0600` permissions.

### Task 2: Gmail gateway, MIME parsing, and classification

- [x] Implement paginated lookback listing, full message retrieval, profile/history calls, attachment reads, and paginated history.
- [x] Recursively parse nested MIME parts and incrementally decode inline or external base64url attachment data.
- [x] Conservatively classify supported PDF/images using trusted senders plus filename/subject medical signals; store ambiguity without user interaction.
- [x] Retry only rate-limit and server failures; map stale history `404` to a recovery signal.

### Task 3: Sync service, CLI, and handoff

- [x] Import likely medical attachments once per immutable message/part revision and retain SHA-256, size, MIME, Gmail IDs, and receipt.
- [x] Advance `historyId` only after a complete successful scan; record Gmail removals; recover stale cursors with the configured lookback.
- [x] Add UI-callable configure/auth/status/sync services and matching CLI commands.
- [x] Document the one-time Gmail Desktop OAuth step and future database importer adapter.
- [x] Run the full test, lint, typing, and migration gates; commit and report.
