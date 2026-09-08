# Free-text Sleep Diary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. User explicitly asks to finish; parallel disjoint archive work is authorized.

**Goal:** Ordinary personal sleep reports become durable diary entries with an unambiguous confirmation.

**Architecture:** Extend existing SleepCoach classification only; retain PilotStore unique source keys and morning-prompt binding. Deterministic saved prefix after actual persistence; no extra model call.

**Tech Stack:** Existing Python SleepCoach, PilotStore, pytest.

## Global Constraints

- No PHI in Git, live writes, provider calls or Telegram sends by implementer.
- Preserve causal-grounding fixes, emergency guard, user-only context, profile isolation and original retry text.
- Preserve unrelated dirty docs/backlog.md, docs/research/, panel/workflows.py and concurrent registry/dashboard work.
- No new commands required, no schema, no new infrastructure, no broad refactor.

## Task 1: Persist standalone reports and confirm saving

**Files:** src/health_agent/pilot/sleep.py, tests/pilot/test_sleep.py; optional small focused test file. Do not change sleep_grounding.py unless demonstrably required; report such need first.

**Interfaces:** existing SleepCoach.handle/PilotStore unchanged. Add conservative predicate for personal declarative sleep/waking statements, reuse existing signals without marking generic health statements as diary. Support natural conjugations (`спал`, `просыпался`, `ночью вставал`, `выспался`, first-person dreamed) and a temporal/personal declarative frame. Bare `сон`, general claims, questions (including no `?` but interrogative wording), unrelated text, third-person report and commands remain non-diary. Explicit /сон remains override. Existing qualifying morning answers retain morning_prompt_key; standalone report never invents one.

- [ ] RED synthetic standalone self-report and replay; no PHI fixtures. Example:
```python
coach.handle(profile, 'Сегодня тяжело просыпался. Ночью вставал два раза.', source_key='synthetic:1', now=NOW)
entries = store.list(profile, 'sleep', 'diary')
assert len(entries) == 1
assert 'morning_prompt_key' not in entries[0].payload
```
- [ ] RED negative cases: `Почему я плохо сплю?`, `Почему я плохо сплю`, `Сон важен для здоровья`, `Мой друг плохо спал`, `Сегодня болит колено`, unknown slash command. Positive with no morning notice, existing morning match, retry original text preserved, provider failure after persistence and empty voice transcription unsaved.
- [ ] Implement is_diary extension and deterministic prefix `Запись сна сохранена.` only after durable write. Keep total reply bounded by existing phone limit; avoid duplicate saved prefix on fallback. When provider path is non-diary it must not assert successful diary saving through application code. Keep original turns and diary storage before provider.
- [ ] GREEN focused sleep/grounding/runtime tests, Ruff/mypy changed files; update only genuinely changed expectations of standalone reports, preserving morning binding tests. Commit owned files, self-review and report RED/GREEN commands/results. Root does full suite, live private probe/backfill and deployment after review.
