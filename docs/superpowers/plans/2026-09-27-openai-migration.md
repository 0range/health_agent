# Health Agent OpenAI migration implementation plan

> Execute inline, task by task, under the user's explicit authorization to migrate every Health Agent module. Other projects are outside scope.

**Goal:** Run all active Health Agent model calls on the saved OpenAI Platform credential; use the verified `gpt-6-astra` for text, images and structured lab extraction, and `gpt-transcribe` for voice.

**Architecture:** Keep domain prompts, storage, evidence validation and deterministic scheduling. Route the shared PilotBrain through the configured provider and a bounded OpenAI Responses adapter. Retain legacy Yandex support for historical results and explicit legacy configurations; never automatically fall back to it. Separate provider consent lists and explicitly configure the authorized owner for OpenAI.

**Tech stack:** Python, existing OpenAI SDK, Pydantic settings, pytest, macOS launchd.

## Constraints

- Only Health Agent; no changes to WHOOP/Qingping collection or other projects.
- Never print or commit credentials. Live probes use synthetic inputs and send no Telegram messages.
- Use the official OpenAI API endpoint, `store=False` for Responses, reasoning `low`, no sampling parameters unsupported by Astra.
- Preserve profile boundaries, source attribution, review gates, output validation and durable error metadata.
- Keep the existing audio size/type limits; verify Telegram Ogg input against the real transcription endpoint.
- Implement in an isolated worktree, test before deploying to the running checkout.

## Task 1: Shared provider boundary and regression coverage

Files: `src/health_agent/ai/openai.py`, `src/health_agent/config.py`, `src/health_agent/pilot/brain.py`, `tests/pilot/test_openai_brain.py`, existing pilot provider fixtures.

- [x] Test every pilot domain with `Settings(_env_file=None, ai_provider="openai", openai_allowed_profile_ids=(profile,))`; assert Responses model, `store=False`, input image data URL, safety identifier and no temperature.
- [x] Test consent denial before loading credentials; reject incomplete, refused and empty responses; preserve sanitized failure metadata.
- [x] Test OpenAI transcription with Ogg bytes and deny unapproved profiles; ensure no Yandex credential or endpoint is used.
- [x] Implement `_OpenAIAdapter._get_client()`, `respond(system, content, profile_id)` returning the response and validated text, and `transcribe(data)` returning nonempty text. Reuse configured key/model/output/reasoning settings, disable SDK retries, redact vendor errors.
- [x] Make PilotBrain choose provider at construction, select provider-specific consent/model, invoke the adapter, and record the actual provider/model. Preserve the legacy path only for `AI_PROVIDER=yandex`.
- [x] Make existing Yandex-specific tests explicit about provider; run the pilot tests with the real domain coaches and mocked SDK transport.

## Task 2: Existing OpenAI entry points and deployment configuration

Files: `src/health_agent/questions/openai.py`, `src/health_agent/lab_extraction/openai.py`, `.env.example`, `tests/questions/test_openai.py`, `tests/lab_extraction/test_openai.py`, operator documentation.

- [x] Set Astra defaults and a 4000-token output budget; preserve low reasoning and bounded timeouts. Pin SDK clients to `https://api.openai.com/v1` so a shell base URL cannot redirect production requests.
- [x] Keep the bounded questions and strict lab JSON contracts; verify actual structured output through a synthetic lab page.
- [x] Document all LLM call sites and the deterministic features that do not call a model.
- [x] Run `python -m pytest tests/pilot tests/questions tests/lab_extraction tests/ai` and the full suite, then Ruff on changed Python files; inspect the complete diff.

## Task 3: Live verification, deploy and push

Files: private `.env` and ignored `data/verification/openai-migration-2026-09-27/` evidence only; no secrets in git.

- [x] Probe Astra text, image, food JSON and correction, sleep/training dialogue, health-question adapter and structured lab extraction with synthetic inputs. Probe Russian Ogg speech with the actual transcription adapter.
- [x] Save a private redacted result report with actual response model/status, latency and usage.
- [ ] Commit reviewed code, fast-forward main, atomically update private `.env` to OpenAI and the authorized owner profile. Keep a private rollback snapshot.
- [ ] Restart main, food, training and panel services; verify new PIDs, fresh poll timestamps and no runtime errors. Check periodic sync configuration and collector health without sending chat messages.
- [ ] Push main and compare local HEAD with origin/main. Report actual tested model and a plain-language call inventory to the user.
