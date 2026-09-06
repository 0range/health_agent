# Yandex Provider Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make the existing question and laboratory workflows selectable between OpenAI and Yandex, without sending live data during implementation.

**Architecture:** Thin Yandex wrappers reuse current prompts/parsers and validation over native Chat (Task 2 supersedes Task 1's initial Responses transport). Provider selection occurs at existing composition boundaries. A Yandex-specific per-profile allowlist guards both calls; no routing framework or DB redesign.

**Status:** Tasks 1–2 complete and independently reviewed. Merged-code synthetic live checks passed; [acceptance report](../reports/2026-09-06-yandex-live-probes.md). Real profile activation remains separate.

**Tech Stack:** Python, Pydantic Settings, existing OpenAI SDK, pytest and disposable PostgreSQL fixtures.

## Global Constraints

- Keep OpenAI working as the default for existing installs. No automatic fallback between vendors.
- Secrets stay in a private local file or environment, never logs or git. No production configuration changes, provider calls or purchases in this code task.
- Separate provider consent from existing OpenAI consent: `YANDEX_ALLOWED_PROFILE_IDS` defaults to an empty JSON array. Both adapters reject unlisted profiles before any network operation. Only explicit informed approval permits adding a real profile. The second user's data is never implicitly included.
- Cloud lab enablement and daily request budget remain necessary in addition to Yandex profile consent. Local processing remains available when cloud is blocked. Yandex results are candidates requiring existing review, never silently verified.
- Extraction job metadata must identify the actual model and provider, not label Yandex results as OpenAI.

### Task 1: Selectable Yandex adapters and offline acceptance

**Files:**
- Modify: `src/health_agent/config.py`, `src/health_agent/questions/composition.py`, `src/health_agent/lab_extraction/service.py`, `src/health_agent/lab_extraction/queue.py`, `src/health_agent/lab_extraction/types.py`, `src/health_agent/lab_extraction/cli.py`.
- Create: `src/health_agent/ai/__init__.py`, `src/health_agent/ai/yandex.py`, `tests/ai/test_yandex.py`, `docs/yandex-ai.md`.
- Extend existing question/lab composition tests where needed. Only tightly scoped modifications to `questions/openai.py` / `lab_extraction/openai.py` are allowed to expose reuse seams; preserve all old defaults.

**Interfaces:**
- Settings adds `ai_provider: Literal['openai','yandex']='openai'`, `yandex_api_key: SecretStr|None=None`, `yandex_api_key_file: Path=Path('.tokens/yandex-api-key')`, `yandex_folder_id: str=''`, `yandex_model: str='qwen3.6-35b-a3b'`, `yandex_allowed_profile_ids: tuple[UUID,...]=()`, corresponding uppercase env aliases; `load_yandex_api_key()` uses existing private-file semantics with Yandex-specific safe errors. Reuse the file loader if refactored, preserving OpenAI tests/messages.
- `YandexResponsesResponder(settings, *, client=None)` implements existing responder protocol; `YandexLabExtractor(settings, *, client=None)` implements existing CloudExtractor protocol. Both check the profile allowlist before constructing a client or calling any network API. Injected clients do not read key files, but do NOT bypass consent.
- Fixed client: `OpenAI(api_key=..., base_url='https://ai.api.cloud.yandex.net/v1', project=folder_id, timeout=30.0, max_retries=0, default_headers={'x-data-logging-enabled':'false'})`. Resolve model as `gpt://{folder_id}/{yandex_model}`; reject blank/invalid folder or model locally (no URI paths/query/newlines in these components).
- Reuse current question input/safety instructions and lab schema/validation. Yandex omits OpenAI reasoning effort rather than sending the default `low`; no tools, conversation IDs, automatic retries or fallback. Preserve `store=False` and bounded output. Do not assume generic OpenAI-compatible fields are proven until a real synthetic test; document this acceptance boundary.
- `_build_responder` and `LabExtractionService` select Yandex only when requested; use the actually selected model in cloud reservation. `ExtractionQueue.publish` gains backward-compatible optional `cloud_method` argument defaulting to `openai_structured`, allowlisted to OpenAI/Yandex tags, and Yandex publishes `yandex_structured`.
- Add safe `yandex_not_configured` and `cloud_provider_consent_required` codes. Consent failure must not make a paid attempt or increment the daily request budget: guard service before reservation while preserving local publish. Existing injected cloud-extractor tests still work with OpenAI settings.
- Add provider-neutral `--cloud/--no-cloud` opt-in to lab CLI, preserving `--openai/--no-openai` behavior for OpenAI only. Reject legacy `--openai` if Yandex selected; never misrepresent whom the user enabled. Update service configure parameter with backwards-compatible alias if needed.

- [x] **Step 1: Add failing tests for settings, profile denial and routing.** Use explicit synthetic settings (`_env_file=None`) to avoid local secrets. Denied tests must prove the injected client's call list stays empty for both adapters and a service-level denied run reserves zero cloud calls while local extraction proceeds.

```python
def test_yandex_requires_explicit_profile_consent():
    settings = Settings(_env_file=None, yandex_folder_id='synthetic-folder')
    client = RecordingClient()
    with pytest.raises(ExtractionError, match='cloud_provider_consent_required'):
        YandexLabExtractor(settings, client=client).extract(UUID(int=1), 'Glucose 5.1 mmol/L')
    assert client.calls == []
```

- [x] **Step 2: Run focused tests red, record failure.** `uv run pytest tests/ai/test_yandex.py -q`.
- [x] **Step 3: Implement the settings, wrappers and wiring above.** Build upon existing adapters; avoid copying the full medical prompt, input builder, lab schema or parser. Preserve existing public constructor signatures/default behavior. Synthetic clients provide completed Responses envelopes; no live endpoint use.
- [x] **Step 4: Cover authorized calls and failures.** Test exact endpoint/project/headers with a patched SDK constructor, key separation, malformed config rejection, authorized vs second-profile denial, output token bound, store flag, absent reasoning, no tools; valid lab JSON with evidence substring accepted, forged evidence/malformed output/refusal/incomplete rejected; timeout/401/429 safe and no retries; actual model and extraction tag recorded by service/queue; CLI legacy and neutral opt-in. Keep OpenAI test behavior unchanged.
- [x] **Step 5: Document short setup and synthetic probe.** `docs/yandex-ai.md` starts with TL;DR and exact env fields. Explain separate folder/service-account API key and billing prerequisites, no tokens in chat/git, no real profile allowlist before consent, health scenarios still blocked until synthetic live test and real data acceptance. Link the official sources from the spec. Include a small synthetic-only Python example using a one-off Settings object with UUID(int=1) allowed and no database, then call lab extractor on invented Glucose text; print only success/count, never provider exception. Do not run it.
- [x] **Step 6: Verify and commit.** Focused tests first, then `uv run pytest -q`, `uv run ruff check .`, `uv run mypy src`, `git diff --check`. Report inherited warnings separately. Stage only owned files and commit. No production settings, API calls, migrations, token files or broad cleanup.

### Task 2: Native Chat Completions compatibility after real synthetic probes

**Files:** modify only `src/health_agent/ai/yandex.py`, `tests/ai/test_yandex.py`, `docs/yandex-ai.md`; adapt Yandex-specific factory tests if necessary. Existing service/queue/consent interfaces and OpenAI remain unchanged.

**Interfaces and exact wire contract:**
- Preserve constructor/respond/extract signatures and current Yandex adapter names to avoid caller migration. Internally call `client.chat.completions.create`, never `.responses.create`. Both still check per-profile consent before client construction/network, and reuse existing fixed endpoint/key/folder/client timeout/no-retries/logging headers.
- Use `model=self.model`, `max_tokens=settings.openai_max_output_tokens`, `reasoning_effort='none'`, `temperature=0`, `store=False`. No `safety_identifier` (observed HTTP400 in live Chat); no `user`, tools, conversation IDs or fallback. Request trace header may remain if supplied. Explicit model changes stay configuration-only, no automatic switching.
- Labs: system message with existing `_INSTRUCTIONS`, user message with original bounded raw `text` (NOT JSON-encoded page_text). Raw source is an untrusted user message, never appended to system instructions. `response_format={'type':'json_schema','json_schema':{'name':'lab_candidates','strict':True,'schema':_SCHEMA}}`. Use existing `validate_candidates(json.loads(content), text)` unchanged. Do not normalize or repair model source fields/excerpts to force acceptance.
- Questions: system message with existing `MEDICAL_SAFETY_INSTRUCTIONS`; reuse `build_responder_input(question, context)` and convert content types from `input_text` to Chat `text`, preserving both bounded JSON content blocks and their separation. No copied prompt/input-builder logic.
- Require exactly one choice, `finish_reason='stop'`, assistant message, nonempty bounded string content, no refusal and no tool/function calls. Maximum accepted content length 80000. Labs map incomplete/length to `cloud_incomplete`, refusal to `cloud_refused`, malformed/tool-call output to `cloud_invalid_output`. Preserve API status safe-code mapping and zero retries; question errors remain safe QuestionResponderError. Untrusted raw response/errors never logged.

- [x] **Step 1: Update tests red.** Native recording fake exposes `chat.completions.create` only. Assert both outbound contracts above, no Responses calls/unsupported safety identifier, raw unchanged lab source, preserved question JSON block boundaries, profile guards before calls. Existing mocked Responses envelopes must no longer be accepted as successful Chat output.
- [x] **Step 2: Implement minimal native Chat adapter path.** A shared local response-content extractor may validate one completed assistant choice for both wrappers; no new providers/framework/settings. Preserve all shared lab validators and OpenAI behavior.

```python
def test_native_chat_requires_a_single_completed_assistant_choice():
    # Reuse the test's synthetic settings and recording client.
    # Parametrize length/refusal/missing-choice/multiple-choice/tool-call envelopes.
    with pytest.raises(ExtractionError):
        extractor.extract(UUID(int=1), 'Glucose 5.1 mmol/L 3.9-5.5')
    assert len(client.chat.completions.calls) == 1
```

- [x] **Step 3: Cover accepted and refused envelopes.** Test exact simple/table/multiline/qualified source evidence against existing validator, bad source excerpt remains rejected, malformed JSON, incomplete finish reason, wrong message role, empty/multiple choices, refusal, tool/function calls, oversize/empty/nonstring content, timeout/401/429 and no retry. Both authorized QA and denial remain tested. No blanket skip/removal of relevant old cases.
- [x] **Step 4: Verify and commit.** Focused `tests/ai/test_yandex.py`, existing question/OpenAI/lab tests, full synthetic suite once, Ruff/mypy/diffcheck. Update short setup guide: native Chat, Qwen reasoning disabled, unsupported safety identifier omitted, headers/store are requests not retention guarantees. Report earlier synthetic live findings as prototype evidence, not acceptance of untested committed code. Root alone runs final real synthetic probes; no key/production reads/calls by implementer.
