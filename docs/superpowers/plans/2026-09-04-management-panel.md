# Local Management Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe, loopback-only page for creating and selecting profiles and viewing/configuring the connector state that the current branch actually supports.

**Architecture:** A small panel service builds profile-scoped, secret-free view models from PostgreSQL and the existing WHOOP, Gmail, and Telegram services. A dependency-free stdlib HTTP adapter renders escaped server-side HTML and accepts only bounded, same-origin form posts; OAuth and synchronization remain explicit CLI/live operations and Google Drive is identified as unavailable until its connector is integrated.

**Tech Stack:** Python 3.13, stdlib `http.server`, SQLAlchemy, existing connector services, Typer, pytest.

## Global Constraints

- Bind only to `127.0.0.1`; reject any non-loopback configured host.
- Never display or accept client secrets, access tokens, refresh tokens, passwords, message bodies, filenames, or medical values.
- Every connector read or local configuration mutation is selected by an existing `profile_id`; never fall back to another profile.
- Do not launch OAuth, run a connector sync, contact a real service, or add a pretend Google Drive implementation.
- Browser mutations use POST, a process-local CSRF token, same-origin checks, bounded request bodies, and redirect-after-post.
- Render per-connector safe error codes without raw exception text; one broken connector must not break the page.
- Add no enterprise authentication or frontend build system.

---

### Task 1: Profile-scoped panel service and connector cards

**Files:**
- Create: `src/health_agent/panel/models.py`
- Create: `src/health_agent/panel/service.py`
- Create: `src/health_agent/panel/__init__.py`
- Test: `tests/panel/test_service.py`

**Interfaces:**
- Produce immutable `ProfileSummary`, `ConnectorCard`, and `ProfilePanel` view models containing only safe display fields.
- Produce `PanelService.list_profiles()`, `PanelService.profile(UUID)`, and `PanelService.create_profile(name)`.
- Production status assembly queries WHOOP accounts by both profile and account, Gmail configuration/state below that profile's local directory, and Telegram status for exactly that profile; Drive returns an explicit `not_available` placeholder.

- [ ] Add tests for ordered profile listing/creation, missing-profile rejection, two-profile status isolation, safe degradation when one connector store is unreadable, and absence of token/medical fields in serialized view models.
- [ ] Implement the view models and dependency-injected service boundary.
- [ ] Implement production WHOOP/Gmail/Telegram adapters using existing status APIs without returning credential values or raw exceptions.
- [ ] Run `uv run pytest -q tests/panel/test_service.py` and commit the task.

### Task 2: Loopback HTTP page and safe local actions

**Files:**
- Create: `src/health_agent/panel/http.py`
- Test: `tests/panel/test_http.py`

**Interfaces:**
- Produce `PanelApplication.handle(method, target, headers, body) -> PanelResponse` for deterministic route tests.
- Produce `serve_panel(service, *, host, port)` using `ThreadingHTTPServer` and exact host `127.0.0.1`.
- Routes: `GET /`, `GET /profiles/<uuid>`, and CSRF-protected `POST /profiles`; unsupported connector actions are explanatory CLI guidance, not network-triggering controls.

- [ ] Add tests for the profile/card page, HTML escaping, profile-not-found, body limit, method handling, CSRF and cross-origin rejection, no secret fields, and refusal to bind non-loopback hosts.
- [ ] Implement escaped Russian-language server-rendered HTML with responsive cards, visible statuses/next actions, accessibility labels, and no external assets.
- [ ] Implement strict routing, bounded form parsing, security/no-store headers, redirect-after-create, and a process-local CSRF token.
- [ ] Implement the stdlib request adapter and loopback-only server factory.
- [ ] Run `uv run pytest -q tests/panel/test_http.py` and commit the task.

### Task 3: CLI entry point, configuration, documentation, and gates

**Files:**
- Modify: `src/health_agent/config.py`
- Modify: `src/health_agent/cli.py`
- Modify: `.env.example`
- Modify: `README.md`
- Create: `docs/management-panel.md`
- Test: `tests/panel/test_cli.py`
- Create: `docs/superpowers/reports/2026-09-04-management-panel-report.md`

**Interfaces:**
- Add validated `PANEL_HOST=127.0.0.1` and `PANEL_PORT=8766` settings.
- Add `health-agent panel serve` that constructs the production panel and prints only the loopback URL.

- [ ] Add CLI/config tests proving defaults, rejection of a public bind address, injected server startup, and secret-free output.
- [ ] Wire the service and HTTP server into the Typer CLI without opening a browser or reading OAuth credentials at command construction time.
- [ ] Document the local URL, implemented actions, connector limitations, explicit OAuth/sync CLI handoff, profile isolation, and lack of live acceptance.
- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src tests`, `uv run alembic heads`, and `git diff --check`.
- [ ] Record exact gate results in the implementation report and commit all remaining documentation.
