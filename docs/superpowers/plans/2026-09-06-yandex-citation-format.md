# Yandex Citation Format Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Make Yandex comply with the existing exact-citation contract on real WHOOP questions.

**Architecture:** Append a static formatting clarification to the existing Yandex QA system message. Keep the shared medical instructions and all validators intact. This is compatibility repair within the approved Yandex design, not a new evidence policy.

**Tech Stack:** Existing Python adapters and pytest; no dependency changes.

**Status:** Complete, independently reviewed and merged in a93e6e4; 1007 tests passed and two actual application questions passed non-fallback citation checks. [Activation report](../reports/2026-09-06-yandex-activation.md).

## Global Constraints

- No evidence normalization, citation repair, relaxed validators, automatic retries or extra model calls.
- OpenAI and lab extraction remain unchanged. Per-profile consent, bounded JSON inputs, output limits and private errors remain unchanged.
- No real data, keys, network calls or production configuration in implementer/reviewer work.
- Root recorded owner-only Yandex consent from the user's continuation after the explicit sharing question. This code task does not extend consent to another profile.

### Task 1: Clarify strict citation syntax

**Files:** Modify only `src/health_agent/ai/yandex.py`, `tests/ai/test_yandex.py`; add targeted cases in `tests/questions/test_service.py` if needed.

**Interfaces:** `_chat_question_messages` must preserve the existing medical system instructions and both question/evidence JSON blocks. Append this static string (constant `_YANDEX_CITATION_INSTRUCTIONS`) to the existing system message only:

```python
_YANDEX_CITATION_INSTRUCTIONS = """
Citation output syntax is strict: copy each supplied bracketed citation label verbatim as a separate token. Never combine IDs inside brackets or abbreviate them into ranges. For multiple sources write [SLEEP1] [SLEEP2], only if both labels were supplied; never [SLEEP1, SLEEP2] or [SLEEP1–SLEEP2]. Square brackets are reserved exclusively for these exact evidence labels. Never put JSON field names, missing-data keys, placeholders, or explanatory text in square brackets. Explain missing data in ordinary Russian prose. Before finalizing, ensure every bracketed token occurs verbatim among the supplied citation labels. Do not invent evidence to satisfy the format.
"""
```

- [x] **Step 1: Add failing outbound-contract test.** Existing authorized QA fake must assert `messages[0]['content'] == MEDICAL_SAFETY_INSTRUCTIONS + _YANDEX_CITATION_INSTRUCTIONS` and that the two original JSON blocks are unchanged. Assert the required positive/negative syntax examples exist in the constant, so a vacuous constant fails. Initial missing import/expectation must fail before implementation; record RED.
- [x] **Step 2: Implement the static suffix only.** Keep all request parameters, lab system text, model choice, client creation and runtime validation unchanged. No dynamically inserted personal data in the suffix.
- [x] **Step 3: Add application regressions.** Synthetic context with allowed labels `[LAB1]` and `[LAB2]`: a fake completed answer using `[LAB1] [LAB2]` is accepted with source footer; `[LAB1–LAB2]`, `[LAB1, LAB2]`, and `[LAB1] [missing_keys]` are each rejected by the existing service. Use actual shared application service and existing synthetic fixture constructors; do not alter its validator. Keep existing tests.

```python
@pytest.mark.parametrize('citations', ['[LAB1–LAB2]', '[LAB1, LAB2]', '[LAB1] [missing_keys]'])
def test_yandex_style_invalid_citation_formats_fail_closed(citations):
    # Use this file's existing FakeContextBuilder/FakeResponder and _context.
    result = HealthQuestionApplicationService(
        FakeContextBuilder(_context()), FakeResponder('Recorded fact. ' + citations)
    ).answer(PROFILE_ID, 'What is recorded?')
    assert result.text.startswith(INSUFFICIENT_EVIDENCE_TEXT)
```

- [x] **Step 4: Verify and commit.** Run focused adapter/service tests, Ruff on changed files, `mypy src`, `git diff --check`. Root runs combined full suite and real acceptance after merge. Record command outputs in the task report; commit only owned files.

## Root acceptance

Observed before fix: two real sleep/recovery questions reached Yandex but were replaced with insufficient-evidence fallback; diagnostic output contained grouped `[WORKOUT1–WORKOUT9]` and internal `[missing_keys]`. A single in-memory suffix prototype produced zero invalid citations and a 3605-character answer including deterministic sources. No patient text or values are recorded here. After merge, run actual unmodified composition again and require a non-fallback answer with valid sources, not merely `available=True`.
