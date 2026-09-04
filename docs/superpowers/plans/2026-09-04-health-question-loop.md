# Health Question Loop v0.1 Implementation Plan

> **For Codex:** Use subagent-driven-development to implement each task with implementation and review reports under `.superpowers/sdd/2026-09-04-health-question-loop/`.

**Goal:** Deliver a profile-isolated, cited health-question loop for CLI and the hardened Telegram poller.

**Architecture:** A read-only SQLAlchemy context builder emits bounded typed evidence. A framework-independent application service sends that evidence to a pluggable responder and deterministically formats citations. The production responder uses OpenAI Responses with private local configuration; a composition module connects it to existing Telegram transport contracts.

**Tech Stack:** Python 3.13, SQLAlchemy, Pydantic Settings, OpenAI Python SDK, Typer, pytest.

**Global constraints:** No real network, OAuth, tokens, or personal data during implementation/tests. No raw health payloads or secrets in logs/status/errors. All retrieval must predicate on `profile_id`; verified labs only. Preserve Telegram fencing/idempotency. Use `apply_patch` for authored changes.

---

## Task 1: Profile-scoped evidence and safety policy

**Files:** Create `src/health_agent/questions/{__init__,models,context,safety}.py`; create `tests/questions/test_context.py` and `test_safety.py`.

Implement typed evidence/context, intent-sensitive bounded windows, verified lab and normalized WHOOP queries, deterministic citation labels, source counts, and an urgent red-flag guard. Test cross-profile exclusion, time boundaries, caps, provenance, missing data, and bilingual urgent phrases.

## Task 2: Application service and OpenAI Responses adapter

**Files:** Create `src/health_agent/questions/{service,openai}.py`; modify `src/health_agent/config.py`, `pyproject.toml`, `uv.lock`; create `tests/questions/test_service.py`, `test_openai.py`, `test_config.py`.

Define the responder protocol and application result/error types. Implement safe prompt construction, deterministic source footer, safe unavailable behavior, `responses.create` with `store=False`, `output_text`, bounded tokens, hashed safety identifier, injectable client, and env/private-file secret loading. Mock all API tests.

## Task 3: Telegram composition and CLI

**Files:** Create `src/health_agent/questions/composition.py`; modify `src/health_agent/cli.py` and Telegram exports only as needed; create `tests/questions/test_cli.py`, `test_composition.py`.

Add `question ask/status` and `telegram run`. Compose verified Telegram credentials/state/messenger/poller with the question service, safe read-only commands, and existing attachment inbox. Ensure startup/status output is secret-free, SIGINT is clean, `/sync` does not mutate, and all factories are injectable for tests.

## Task 4: Integration, documentation, and gates

**Files:** Create `tests/questions/test_question_loop_integration.py`, `docs/health-questions.md`, and `docs/superpowers/reports/2026-09-04-health-question-loop-report.md`; update `README.md` minimally.

Exercise a bound-profile free-form Telegram update through retrieval, mocked responder, cited reply, and existing delivery state. Run focused tests, full `pytest`, `ruff check`, `mypy`, and `alembic check`. Record exact evidence and remaining live-only validation, then request whole-branch independent review.
