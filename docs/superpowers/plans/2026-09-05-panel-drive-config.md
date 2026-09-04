# Management Panel Google Drive Configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Configure or replace a selected profile's Google Drive source folder in the localhost management panel.

**Architecture:** Add a Drive configuration port and reader to `PanelService`, backed by existing Drive profile/state stores. Add one protected profile-scoped HTTP form and render only safe configuration state.

**Tech Stack:** Python 3.12, stdlib HTTP server, SQLAlchemy profile repository, existing Google Drive local stores, pytest.

## Global Constraints

- Keep the panel bound to `127.0.0.1` and preserve exact Host, Origin, CSRF, strict-route, and 4 KiB body checks.
- Never invoke OAuth, Drive network APIs, live sync, or production database mutations.
- Never render OAuth tokens or client secrets.
- A folder change replaces the v0.1 root list, preserves verified account binding, and clears only that profile's cursor.

---

### Task 1: Profile-scoped Drive service

**Files:**
- Modify: `src/health_agent/panel/service.py`
- Test: `tests/panel/test_service.py`

**Interfaces:**
- Consumes: `DriveProfile.create`, `LocalProfileStore`, `LocalSyncStateStore`
- Produces: `PanelService.configure_drive(profile_id: UUID, folder: str) -> None`, `DriveStatusReader.cards(profile_id)`

- [ ] Write failing service tests for profile existence, isolation, account binding, cursor reset, and safe status.
- [ ] Run `uv run pytest tests/panel/test_service.py -q` and confirm failures.
- [ ] Implement the minimal configuration port and Drive reader.
- [ ] Run the focused service tests and confirm they pass.

### Task 2: Protected panel form

**Files:**
- Modify: `src/health_agent/panel/http.py`
- Test: `tests/panel/test_http.py`
- Modify: `docs/management-panel.md`

**Interfaces:**
- Consumes: `PanelService.configure_drive`, `ProfilePanel`
- Produces: `POST /profiles/{uuid}/drive` and a Drive form on `GET /profiles/{uuid}`

- [ ] Write failing HTTP tests for successful canonical URL update and explicit safe messages.
- [ ] Add rejection tests for CSRF, origin, unknown profile, invalid form fields, hostile URL, and bodies over 4 KiB.
- [ ] Run `uv run pytest tests/panel/test_http.py -q` and confirm failures.
- [ ] Implement strict route parsing, bounded form parsing, safe response rendering, and documentation.
- [ ] Run all panel tests and confirm they pass.

### Task 3: Quality gates

**Files:**
- Modify only files required by failed checks.

**Interfaces:**
- Consumes: completed service and HTTP tasks
- Produces: a reviewed, committed feature branch

- [ ] Run `uv run pytest -q`.
- [ ] Run `uv run ruff check .`.
- [ ] Run `uv run mypy src`.
- [ ] Inspect `git diff --check` and the complete diff for secret leakage and profile-boundary bugs.
- [ ] Commit the implementation without merging or pushing main.
