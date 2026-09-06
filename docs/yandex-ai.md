# Yandex AI Studio setup

TL;DR: Yandex is an optional, explicitly selected provider. It is disabled for every
profile by default. Merged adapters passed a small synthetic live extraction/QA probe
([results](superpowers/reports/2026-09-06-yandex-live-probes.md)); real health-data
sharing still requires the specific profile owner's acceptance.

Set these exact environment fields (or use the private key file):

```dotenv
AI_PROVIDER=yandex
YANDEX_API_KEY=service-account-api-key
YANDEX_API_KEY_FILE=.tokens/yandex-api-key
YANDEX_FOLDER_ID=your-folder-id
YANDEX_MODEL=qwen3.6-35b-a3b
# Optional QA-only override; omit to reuse YANDEX_MODEL.
# YANDEX_QUESTION_MODEL=question-model-id
YANDEX_QUESTION_TIMEOUT_SECONDS=30
YANDEX_ALLOWED_PROFILE_IDS=[]
```

The Yandex key is separate from `OPENAI_API_KEY`. The folder must have AI Studio access
and active billing. For native Chat Completions, grant the service account the
`ai.languageModels.user` role and create its API key with the
`yc.ai.foundationModels.execute` scope. Yandex's newer key UI may show more granular
language-model permissions; follow the current official authentication page if the UI
has changed. Never put a token in chat or git. If using `YANDEX_API_KEY_FILE`, make it a
regular file with mode `0600`. Keep the profile allowlist empty until informed consent
is recorded; cloud lab processing also requires the provider-neutral `--cloud` opt-in
and retains its daily budget.

`YANDEX_QUESTION_MODEL` changes only health-question answers; laboratory
extraction continues to use `YANDEX_MODEL`. The QA-only timeout defaults to 30
seconds and accepts 1–60 seconds. Both clients keep automatic retries disabled.
Use an alternative question model only after synthetic and owner-approved
acceptance testing; the example does not activate or recommend a production
model by itself.

The adapter uses Yandex's native Chat Completions compatibility surface. Earlier
synthetic prototype probes showed that Qwen works with `reasoning_effort=none` and that
the Responses-style safety identifier causes an HTTP 400, so the adapter disables
reasoning and omits that unsupported field. Those prototype findings are evidence for
the wire contract; the merged adapter has since passed the linked synthetic smoke.
Run the probe below when setting up another installation. `store=False` and the logging opt-out header are
requests to the provider, not guarantees of zero retention. Health scenarios remain
blocked until that probe and explicit acceptance for real data.

Synthetic-only probe (invented text, one-off settings, no database):

```python
from uuid import UUID

from health_agent.ai.yandex import YandexLabExtractor
from health_agent.config import Settings

profile_id = UUID(int=1)
settings = Settings(
    _env_file=None,
    ai_provider="yandex",
    yandex_folder_id="your-folder-id",
    yandex_allowed_profile_ids=(profile_id,),
)
try:
    candidates = YandexLabExtractor(settings).extract(
        profile_id, "Glucose 5.1 mmol/L"
    )
except Exception:
    print("success=false")
else:
    print(f"success=true count={len(candidates)}")
```

Official references: [OpenAI-compatible Chat Completions](https://yandex.cloud/en/docs/foundation-models/concepts/openai-compatibility),
[authentication and required roles](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/authentication),
[logging-header caveat](https://aistudio.yandex.ru/docs/en/ai-studio/sdk-ref/sync/sdk.html),
and [model URI announcement](https://yandex.cloud/en/blog/digest-april-2026).
