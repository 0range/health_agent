# Sleep Grounding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. User explicitly requests implementation and parallel work; no new approval gate.

**Goal:** Stop unrelated historical labs and prior assistant claims from becoming current sleep/fatigue explanations.

**Architecture:** Focused selection at SleepCoach context boundary, bounded output validation/rendering for causal symptom questions; preserve unrestricted archival storage and existing general-health path. No new service/model.

**Tech Stack:** Python, existing PilotStore/HealthContextBuilder/PilotBrain, pytest.

## Global Constraints

- No fabricated dates, numeric repairs, disease exclusion from wearables or reference-range labs. If cause is not established, explicitly say it is unknown.
- Preserve source-specific dates, original user statements, profile isolation, emergency guard, diary/goals/idempotency, general medical questions.
- Preserve stored conversations and clinical rows; no live writes/provider calls by implementer, no PHI in Git. Preserve unrelated docs/backlog.md, docs/research/, src/health_agent/panel/workflows.py.
- No model switch, food/training changes, new infrastructure or broad refactor. Root handles archive operations independently.

## Task 1: Ground the actual sleep coach path

**Files:** src/health_agent/pilot/sleep.py, new src/health_agent/pilot/sleep_grounding.py if needed to isolate policy; src/health_agent/pilot/runtime.py only the sleep context adapter; tests/pilot/test_sleep.py, tests/pilot/test_runtime.py and one focused grounding test file. questions/context.py only if a minimal shared intent recognition change is required; no unrelated general evidence redesign.

**Interfaces:** retain SleepCoach.handle/health_context callable and durable Store protocol. New pure helpers may consume question, dated user turns/diary, bounded build_responder_input JSON, and now. Runtime must pass effective question context consistently. Implementer specifies pure helper signatures in report. Classify sleep/fatigue including Russian 'спать хочу', 'спал', 'устал', 'сонливость', mixed infection/weather questions and follow-ups using prior USER text (never previous assistant medical claims). Current question remains current; relevant user denials preserved.

- [ ] RED using FakeStore/FakeBrain existingfixtures: user asks why sleepy recently, health snapshot contains old CRP plus fresh WBC and irrelevant knee/insurance reports, conversation has assistant misdated CRP. Actual sent payload must omit old/unrelated evidence and assistant-generated medical prose, retain recent sleep/recovery and relevant user reports. Test no future/undated medical result passed as current; no mutation of original context/store.
```python
assert '2099-01-01' not in encoded_current_evidence
assert 'historical_marker_sentinel' not in encoded_current_evidence
assert 'unsupported_assistant_claim_sentinel' not in encoded_prompt
assert original_context == before_context
```
- [ ] RED follow-up with user 'есть новые анализы, температуры нет': fresh labs retain each own ISO date; old CRP cannot receive newest report date; recent user denial retained, no repeated temperature question. Explicit historical lab question can access historical facts labeled historical; purely sleep question does not dump labs.
- [ ] Implement minimal focused evidence policy and user-only attributed conversation context (stored assistant history remains untouched). Avoid duplication between raw observations and snapshot where possible. No report contamination in focused sleep path. Do not filter unrelated medical queries globally.
- [ ] RED causal-answer boundary: model output asserting 'это не инфекция' or transplanting a historical result/date must not be delivered. Use constrained causal reply format with application-rendered uncertainty and source-derived observation rather than trusting arbitrary diagnosis prose or citation existence. Fake output unknown/malformed/unsupported selected fact -> useful honest fallback, not provider stacktrace. One selected fact maximum, own date, one question; do not introduce unrelated tests/diagnoses. Other diary/general question responses retain concise natural language with strict relevance instructions.
```python
assert 'это не инфекция' not in reply.lower()
assert len(reply) <= 1200
assert any(word in reply.lower() for word in ('непонятно', 'нельзя определить', 'не могу определить', 'не установлена', 'неизвестна'))
```
- [ ] GREEN focused sleep/runtime/grounding tests and Ruff/mypy touchedfiles; report actualRED/GREEN. Self-review and commitownedonly. Root runs fullsuite once after all changes, independentreview then realmodelprobe withoutTelegramdelivery.

## Root completion

- [ ] Read actual stored model_run inputs/outputs, keep reproduction private. No need to ask user to resubmit.
- [ ] Task review plus whole-delta final review; finish archive recovery parallel under existing source/attempt controls.
- [ ] Run real model probes of original question and follow-up with a non-persisting/in-memory test store; check actual short output/date/uncertainty/no repeated deniedsymptom question. No unsolicitedTelegrammessages.
- [ ] Deploy/restart mainbot, verify freshpoll; push main+workingbranch and report actualremainingarchiveblockers, not merelytechnicaltestcounts.
