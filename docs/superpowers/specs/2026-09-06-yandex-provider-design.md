# Yandex AI Studio: bounded provider replacement

TL;DR: keep the local database, integrations, prompts, validation and Telegram. Add Yandex AI Studio/Qwen as an explicitly selected alternative for questions and lab extraction. Test synthetic data first; live medical sharing remains off until separately approved.

The user selected Yandex AI Studio on 2026-09-06 after discussing local Ollama and GigaChat alternatives, and requested continued implementation. This is the selected adapter design, not a new product architecture or a reduction of v0.1 scope.

## Contract

- Keep OpenAI working as the default for existing installs. No automatic fallback between vendors.
- Add `AI_PROVIDER=openai|yandex`, separate Yandex key/key-file, folder ID and model name (`qwen3.6-35b-a3b`). Use the existing OpenAI SDK with the fixed official Yandex endpoint; no extra framework or dependency.
- Use stateless Responses requests and the existing bounded prompts, evidence validation, structured laboratory output and safe error handling. Request `store=False` and disabled data logging; do not claim guaranteed zero retention from these flags.
- Separate provider consent from existing OpenAI consent: `YANDEX_ALLOWED_PROFILE_IDS` defaults to an empty JSON array. Both adapters reject unlisted profiles before any network operation. Only explicit informed approval permits adding a real profile. The second user's data is never implicitly included.
- Secrets stay in a private local file or environment, never logs or git. No production configuration changes, provider calls or purchases in this code task.
- Cloud lab enablement and daily request budget remain necessary in addition to Yandex profile consent. Local processing remains available when cloud is blocked. Yandex results are candidates requiring existing review, never silently verified.
- Extraction job metadata must identify the actual model and provider, not label Yandex results as OpenAI.

## Acceptance

Synthetic adapter and composition tests prove selection, separate credentials, profile denial before network, bounded stateless requests, structured-output validation, redacted errors and unchanged OpenAI behavior. A separate synthetic live smoke test requires the user's Yandex folder/key and working billing; it does not establish medical answer quality. Production activation requires that smoke test and consent.

## Live-driven correction (2026-09-06)

The user requested comparison of other Yandex models and document OCR. Real synthetic tests found that the Responses path returned wrong lab fields/incomplete outputs with Qwen and GPT-OSS. Qwen's native Chat Completions path with `reasoning_effort='none'`, `temperature=0`, raw source in its own untrusted user message, strict JSON schema and `store=False` passed simple/table/multiline/qualified lab rows; factual QA cited evidence and identified absent sleep data. Chat rejects `safety_identifier` with HTTP400, while the same request without that field succeeds. Therefore Yandex alone moves to Chat Completions; OpenAI remains unchanged. Existing profile consent/isolation, input limits, source validator and cloud budget remain mandatory. No automatic fallback or weaker evidence validation. The public adapter class name may remain for backwards compatibility, with its transport documented accurately.

## Sources checked 2026-09-06

- Official model URI: https://yandex.cloud/en/blog/digest-april-2026
- Official SDK/endpoint/project example: https://aistudio.yandex.ru/ru/docs/ai-studio/operations/generation/multimodels-request-responses
- Responses API: https://aistudio.yandex.ru/ru/docs/ai-studio/api/Responses/createResponse
- Logging header caveat (supported backends only): https://aistudio.yandex.ru/docs/en/ai-studio/sdk-ref/sync/sdk.html
