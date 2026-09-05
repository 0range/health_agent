# Telegram Russian UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every existing user-visible Telegram reply consistently Russian while preserving the bot's safety, citation, retry-byte, and profile-isolation contracts.

**Architecture:** Keep command names, machine statuses, safe error codes, and transport behavior unchanged. Update only deterministic presentation strings and the existing OpenAI safety instruction, with focused contract tests at each boundary before implementation.

**Tech Stack:** Python 3.13, pytest, SQLAlchemy, OpenAI Responses adapter, Ruff, mypy, uv

## Global Constraints

- Add no commands, features, live service calls, dependencies, or schema changes.
- Preserve citation validation, deterministic footer ordering, exact prepared-reply replay bytes, security boundaries, retry behavior, and profile isolation.
- Keep technical CLI/log statuses and safe error codes in English.
- Tell OpenAI to answer in Russian unless the user clearly requests another language.

---

### Task 1: Health-question copy and responder language

**Files:**
- Modify: `tests/questions/test_service.py`
- Modify: `tests/questions/test_safety.py`
- Modify: `tests/questions/test_openai.py`
- Modify: `src/health_agent/questions/service.py`
- Modify: `src/health_agent/questions/safety.py`
- Modify: `src/health_agent/questions/openai.py`

**Interfaces:**
- Consumes: `HealthQuestionApplicationService.answer(...)` and `OpenAIResponsesResponder.respond(...)`.
- Produces: Russian safe/insufficient/urgent copy, Russian deterministic footer labels, and an explicit language rule in `MEDICAL_SAFETY_INSTRUCTIONS`.

- [x] **Step 1: Write failing focused tests**

```python
assert result.text.startswith("Сейчас не хватает проверенных данных")
assert "Источники:" in result.text
assert "Ограничения:" in result.text
assert "Отвечай по-русски" in call["instructions"]
```

- [x] **Step 2: Run focused tests and verify the old English copy fails**

Run: `uv run pytest tests/questions/test_service.py tests/questions/test_safety.py tests/questions/test_openai.py -q`

- [x] **Step 3: Make the minimum presentation-only implementation**

```python
QUESTION_UNAVAILABLE_TEXT = "Сейчас не удалось ответить на вопрос о здоровье. Попробуйте ещё раз позже."
```

Translate the fixed footer scaffolding and urgent response; add the conditional Russian-output instruction without changing JSON input, bounds, citations, or request options.

- [x] **Step 4: Run the focused tests until they pass**

Run: `uv run pytest tests/questions/test_service.py tests/questions/test_safety.py tests/questions/test_openai.py -q`

### Task 2: Telegram command, import, and review copy

**Files:**
- Modify: `tests/telegram/test_service.py`
- Modify: `tests/telegram/test_review.py`
- Modify: `tests/telegram/test_capture_integration.py`
- Modify: `tests/questions/test_question_loop_integration.py`
- Modify: `src/health_agent/questions/composition.py`
- Modify: `src/health_agent/telegram/review.py`

**Interfaces:**
- Consumes: existing `/status`, `/sync`, attachment ingestion, and review command routes.
- Produces: Russian status/sync, capture/import acknowledgements, and review prompts/outcomes with unchanged command syntax and IDs.

- [x] **Step 1: Update focused expectations before production copy**

```python
assert reply.startswith("Состояние данных о здоровье:")
assert "получен" in receipt.reply_text
assert confirmation == f"Показатель {item_id} подтверждён."
```

- [x] **Step 2: Run focused Telegram tests and verify failures**

Run: `uv run pytest tests/telegram/test_service.py tests/telegram/test_review.py tests/telegram/test_capture_integration.py tests/questions/test_question_loop_integration.py -q`

- [x] **Step 3: Translate only existing reply paths**

Keep slash commands, UUIDs, source counts, and local CLI command invocations byte-for-byte except for surrounding Russian explanatory text.

- [x] **Step 4: Run focused Telegram tests until they pass**

Run: `uv run pytest tests/telegram/test_service.py tests/telegram/test_review.py tests/telegram/test_capture_integration.py tests/questions/test_question_loop_integration.py -q`

### Task 3: Repository verification and review package

**Files:**
- Create: `docs/superpowers/reports/2026-09-05-telegram-russian-ux-report.md`

**Interfaces:**
- Consumes: the completed presentation-only diff.
- Produces: reproducible verification evidence and explicit unvalidated-live boundaries.

- [x] **Step 1: Run the full test and static-analysis suite**

```bash
uv run pytest
uv run ruff check .
uv run mypy src
uv lock --check
```

- [x] **Step 2: Audit the final diff and user-visible strings**

```bash
git diff --check
git diff --stat 4d5eca9...HEAD
rg -n 'Sources:|Limitations:|Health-question|Usage:|Unverified item|Confirmed item|Medical PDF|Medical image' src/health_agent/questions src/health_agent/telegram
```

- [x] **Step 3: Write the report with exact commands/results**

Document scope, tests, unchanged safety/retry/profile contracts, and that no live Telegram/OpenAI calls were made.

- [x] **Step 4: Commit the verified implementation**

```bash
git add src tests docs/superpowers/plans docs/superpowers/reports
git commit -m "feat: localize Telegram UX in Russian"
```
