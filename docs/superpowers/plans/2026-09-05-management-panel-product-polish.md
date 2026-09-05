# Management Panel Product Polish Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the existing local panel into a concise Russian daily overview for each profile while preserving safe local management workflows.

**Architecture:** Extend the existing immutable panel view model with safe destinations and add two local-only status readers for reminders and PostgreSQL. Keep all rendering server-side in the existing HTTP module, translating connector states into three product states and putting identifiers, timestamps, codes, and CLI guidance behind native collapsed details.

**Tech Stack:** Python 3.13, SQLAlchemy 2, stdlib HTTP server and HTML escaping, pytest, Ruff, mypy, Playwright CLI.

## Global Constraints

- Bind and link only to validated loopback destinations; perform no connector or dashboard network request.
- Do not expose secrets, health values, reminder text, raw documents, filenames, message bodies, or provider payloads.
- Preserve existing profile creation and Drive root configuration routes and validation.
- Do not change CLI, configuration fields, database schema, migrations, OAuth, connector sync, Sheets, or capture code.
- Render responsive, semantic, keyboard-accessible HTML without JavaScript or external assets.
- Keep every profile status query explicitly profile-scoped.

---

### Task 1: Add safe local system status and destinations

**Files:**
- Modify: `src/health_agent/panel/models.py`
- Modify: `src/health_agent/panel/service.py`
- Test: `tests/panel/test_service.py`

**Interfaces:**
- Consumes: existing `ReminderRepository.status(profile_id)`, `SessionScopeFactory`, and `Settings.metabase_url`.
- Produces: `PanelDestination(key: str, label: str, url: str | None, unavailable_text: str | None)`, `ProfilePanel.destinations`, `ReminderStatusReader.cards(profile_id)`, and `DatabaseStatusReader.cards(profile_id)`.

- [ ] **Step 1: Write failing profile-safe reader tests**

Add tests which create different reminder aggregates for two profiles and assert
that the `reminders` card exposes only the selected profile's three counts. Add a
database-reader test that expects a `database/ready` card and a failure-isolation
test through `PanelService._safe_cards`.

```python
first_card = ReminderStatusReader(sessions).cards(first_id)[0]
second_card = ReminderStatusReader(sessions).cards(second_id)[0]
assert first_card.detail == "Ожидают подтверждения: 1 · Запланировано: 2 · Пора отправить: 0"
assert second_card.detail == "Ожидают подтверждения: 0 · Запланировано: 0 · Пора отправить: 0"
assert "private title" not in repr(first_card)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/panel/test_service.py -q`

Expected: collection/import failure because the new readers do not exist.

- [ ] **Step 3: Implement the minimal safe readers and destination model**

Add the immutable destination DTO and default `destinations=()` field. Implement
the reminders reader with `ReminderRepository(session).status(profile_id)` and
only aggregate counts. Implement the database reader with `session.execute(select(1))`.
Pass both readers from `build_panel_service`; pass a validated Metabase destination
from `settings.metabase_url` and a `google_sheets` destination with `url=None`.

```python
return ConnectorCard(
    "reminders",
    "action_required" if summary.pending_confirmation or summary.due else "ready",
    f"Ожидают подтверждения: {summary.pending_confirmation} · "
    f"Запланировано: {summary.scheduled} · Пора отправить: {summary.due}",
)
```

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `uv run pytest tests/panel/test_service.py -q`

Expected: all panel service tests pass.

- [ ] **Step 5: Commit the local status model**

```bash
git add src/health_agent/panel/models.py src/health_agent/panel/service.py tests/panel/test_service.py
git commit -m "feat: add panel system status overview"
```

### Task 2: Render the product-level profile overview

**Files:**
- Modify: `src/health_agent/panel/http.py`
- Test: `tests/panel/test_http.py`

**Interfaces:**
- Consumes: `ProfilePanel.connectors`, `ProfilePanel.destinations`, existing `_cli_guidance`, and escaped profile/connector values.
- Produces: `_product_status(card) -> Literal["connected", "not_synced", "action_required"]`, Russian connector labels, safe destination rendering, and responsive semantic HTML.

- [ ] **Step 1: Write failing content, escaping, and accessibility tests**

Assert the main layer uses Russian names and the three user states, does not show
UUID/account/error/CLI text until inside a collapsed `<details>`, includes one
`h1`, labelled sections, visible status text, safe Metabase anchor attributes, and
a non-anchor Google Sheets placeholder. Include hostile profile, detail, account,
error, and destination values and assert all are escaped.

```python
assert "Подключено" in page
assert "Нужно действие" in page
assert '<details class="technical-details">' in page
assert f">{profile.id}<" not in page.split("<details", 1)[0]
assert 'href="http://127.0.0.1:53000"' in page
assert "Появится после подключения Google Таблицы" in page
assert 'aria-labelledby="system-status"' in page
```

- [ ] **Step 2: Run HTTP tests and verify RED**

Run: `uv run pytest tests/panel/test_http.py -q`

Expected: assertions fail against the current raw connector cards.

- [ ] **Step 3: Implement product state mapping and safe link rendering**

Map ready states with a previous success to `connected`, configured/ready without
a success to `not_synced`, and every auth/error/unavailable state to
`action_required`. Use stable connector names `WHOOP`, `Google Drive`, `Gmail`,
`Telegram`, `Напоминания`, and `Локальная база`. Validate destination URLs again
at render time: Metabase must be HTTP(S) loopback without credentials/query/
fragment; Google Sheets must be HTTPS `docs.google.com/spreadsheets/d/<opaque-id>`.
Invalid values render unavailable text, never an anchor.

- [ ] **Step 4: Implement semantic responsive presentation**

Refactor `_page`, `_render_home`, `_render_profile`, and `_render_card` only.
Add a compact roll-up, state pills with text and colour, a responsive grid, 44px
controls, visible `:focus-visible`, native technical details, destination cards,
and a secondary collapsed Drive settings section. Preserve form action, field
names, CSRF token, textarea value, notices, and all routes byte-for-byte where
tests depend on them.

- [ ] **Step 5: Run HTTP and complete panel tests**

Run: `uv run pytest tests/panel -q`

Expected: all panel tests pass, including existing security and workflow tests.

- [ ] **Step 6: Commit the polished renderer**

```bash
git add src/health_agent/panel/http.py tests/panel/test_http.py
git commit -m "feat: polish panel profile overview"
```

### Task 3: Document and visually verify desktop/mobile workflows

**Files:**
- Modify: `docs/management-panel.md`
- Create: `output/playwright/panel-desktop.png` (ignored verification artifact)
- Create: `output/playwright/panel-mobile.png` (ignored verification artifact)
- Test: `tests/panel/test_http.py`

**Interfaces:**
- Consumes: the loopback `serve_panel` boundary and a fake `PanelService`; no production settings or data.
- Produces: checked desktop/mobile screenshots and an updated daily-user runbook.

- [ ] **Step 1: Update the runbook**

Document the three user states, six cards, collapsed technical details, Metabase
link, Google Sheets placeholder, preserved Drive editor, and the strict read-only
status behavior. Keep CLI troubleshooting commands as a secondary details path.

- [ ] **Step 2: Verify Playwright CLI prerequisites**

Run: `command -v npx >/dev/null 2>&1`

Expected: exit code 0. Then use
`/Users/vitali.arz/.codex/skills/playwright/scripts/playwright_cli.sh`.

- [ ] **Step 3: Start a fake-data loopback panel**

Run a bounded Python process that constructs `PanelService` from fake profile and
reader objects, starts `serve_panel(..., host="127.0.0.1", port=0)`, prints its
assigned URL, and calls `serve_forever()`. Fake details contain no health or secret
values.

- [ ] **Step 4: Capture and inspect desktop behavior**

Open the printed URL headed, snapshot before using refs, set viewport to 1440x1000,
open the first profile, capture `output/playwright/panel-desktop.png`, open one
`Подробности` control by its current snapshot ref, and re-snapshot. Assert through
the snapshot and screenshot that six cards, destinations, and the Drive editor are
visible without horizontal overflow.

- [ ] **Step 5: Capture and inspect mobile behavior**

Set viewport to 390x844, reload, capture `output/playwright/panel-mobile.png`, and
verify cards form one column, controls remain visible, and there is no horizontal
overflow. Use a fresh snapshot after each navigation or substantial DOM change.

- [ ] **Step 6: Run final repository gates**

Run:

```bash
uv run pytest -q
uv run ruff check .
uv run mypy .
uv lock --check
git diff --check
```

Expected: all gates pass, or only failures independently reproduced on base
`18d97a3` are recorded in the handoff.

- [ ] **Step 7: Commit docs and prepare review package**

```bash
git add docs/management-panel.md
git commit -m "docs: explain daily management panel"
git diff --binary 18d97a3...HEAD > .superpowers/sdd/2026-09-05-panel-polish/review-18d97a3..HEAD.diff
```

The ignored review package must contain no token, profile data, screenshot, or
external response. Do not merge or push the branch.
