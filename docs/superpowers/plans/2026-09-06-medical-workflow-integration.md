# Medical workflow integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Make reviewed visits and recurring reminders usable through the existing Telegram bot and local panel, with one deployable schema head.

**Architecture:** Register existing application boundaries; add one small panel workflow adapter that reuses the same deterministic commands and repositories. Keep local visit records independent of Calendar authorization. Calendar publication is the next independent integration, not an implicit effect of saving a local visit.

**Tech Stack:** Existing Python, SQLAlchemy, Typer, server-rendered local HTTP panel, pytest with disposable PostgreSQL.

## Global Constraints

- User explicitly requested parallel isolated implementation and no user checks until development completes; do not ask for another planning approval.
- Preserve local PostgreSQL/vault, profile isolation, source fidelity, existing loopback Host/Origin/CSRF checks, and prepared Telegram reply idempotence.
- Do not call live services, read credentials, create synthetic production records, or alter other worktrees.
- No new framework, dependencies, medical interval guesses, implicit Calendar writes, or replacement of free health questions with a fixed menu.
- Same form retry must reuse the action identity; modified replay must fail safely. Escape all displayed user text. No raw exception/SQL responses.

### Task 1: Compose visits and deliver daily-use forms

**Files:** Modify `src/health_agent/cli.py`, `src/health_agent/questions/composition.py`, `src/health_agent/panel/http.py`, `src/health_agent/panel/service.py`, `tests/test_schema.py`; create `src/health_agent/panel/workflows.py`, `tests/panel/test_workflows.py`, `tests/test_medical_workflow_composition.py`, `alembic/versions/0011_medical_workflows.py`, `docs/medical-workflows.md`. Narrow help-text updates are allowed in existing Telegram question command module. Avoid Calendar package and lab parser files.

**Interfaces:** Existing `DatabaseVisitCommands(engine).handle(MessageContext,text)` and `DatabaseReminderCommands(engine).handle(...)` return safe Russian text. `visits.cli.app` is the Typer child. `PanelService` already owns profile repository and sessions; add an optional workflow adapter dependency to preserve existing tests/fakes. Expose profile-checked `workflow_snapshot(profile_id)` and `workflow_action(profile_id, fields)`; service rejects unknown profiles before any action. `MessageContext` must be constructed using its real required fields, with locally namespaced stable action identity; do not invent a Telegram user binding for browser users. Prefer direct repositories if context identity cannot safely represent panel actions.

- [ ] Step 1: Add failing composition and UI tests. Required concrete checks:

```python
def test_single_schema_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    assert ScriptDirectory.from_config(Config('alembic.ini')).get_heads() == ['0011_medical_workflows']
```

Also test Telegram composed handlers accept `/visits` while an ordinary sleep question reaches the existing responder; CLI help contains `visit`; panel GET workflow is read-only, invalid Host/Origin/CSRF cannot write, an unknown profile returns404, note HTML escapes, repeated same form creates one visit, altered same identity fails, profile-B cannot mutate profile-A visit. No assertion-only smoke test instead of actual persisted effects.

- [ ] Step 2: Run new tests and record genuine missing-feature failures before implementation.

- [ ] Step 3: Register visit CLI and handler, remove only the existing duplicate `engine_factory(settings)` assignment in Telegram composition. Create merge migration:

```python
revision = '0011_medical_workflows'
down_revision = ('0009_reminder_recurrence', '0010_doctor_visits')
branch_labels = None
depends_on = None
def upgrade():
    pass
def downgrade():
    pass
```

Update only schema-head assertions from0008 to0011, preserving their downgrade targets and data-loss checks.

- [ ] Step 4: Add `/profiles/{uuid}/medical` GET and POST using existing panel security shell and link it from profile overview. Render upcoming/recent visits(max20), active reminders(max20), readable empty states, and forms: create visit(title,time), add question/answer(select visit code,text), prepare/done/cancel visit; create reminder(title,time,optional days/months count), confirm/done/cancel reminder. Moscow timezone is stated visibly. Each form includes existing CSRF token and a fresh random action identity; persist/reuse that identity in the relevant repository action key so browser retry cannot duplicate an action. Do not repurpose global bot IDs or use timestamps as unique IDs. Bounded forms ≤16KiB, note≤10000 chars; reject repeated form keys and unknown operations. GET may read notes but must not auto-add preparation questions. On successful POST render a safe result and refreshed state; no raw external URLs or vault paths. Keep Calendar status honest: saved locally, Calendar not automatically published. Use existing visual style, large labelled controls, no JSON editor.

- [ ] Step 5: Run `uv run pytest tests/panel tests/visits tests/reminders tests/questions tests/test_schema.py tests/test_medical_workflow_composition.py -q`, `uv run ruff check` on changed source/tests and `uv run mypy src`; record outputs. All database fixtures must remain disposable. Commit owned files and report integration limitations explicitly; no live deployment in implementer.
