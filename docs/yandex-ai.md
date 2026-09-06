# Yandex AI Studio setup

TL;DR: Yandex is an optional, explicitly selected provider. It is disabled for every
profile by default, and this repository has only synthetic offline acceptance. Do not
send real health data until a synthetic live probe succeeds and the specific profile
owner separately accepts the data-sharing boundary.

Set these exact environment fields (or use the private key file):

```dotenv
AI_PROVIDER=yandex
YANDEX_API_KEY=service-account-api-key
YANDEX_API_KEY_FILE=.tokens/yandex-api-key
YANDEX_FOLDER_ID=your-folder-id
YANDEX_MODEL=qwen3.6-35b-a3b
YANDEX_ALLOWED_PROFILE_IDS=[]
```

The Yandex key is separate from `OPENAI_API_KEY`. The folder must have AI Studio access
and active billing. For the Responses API, grant the service account the
`ai.assistants.editor` and `ai.languageModels.user` roles and create its API key with the
`yc.ai.foundationModels.execute` scope. Yandex's newer key UI may show more granular
language-model permissions; follow the current official authentication page if the UI
has changed. Never put a token in chat or git. If using `YANDEX_API_KEY_FILE`, make it a
regular file with mode `0600`. Keep the profile allowlist empty until informed consent
is recorded; cloud lab processing also requires the provider-neutral `--cloud` opt-in
and retains its daily budget.

The adapter uses the documented Responses-compatible endpoint, but compatibility of
every request field remains an acceptance boundary until a synthetic live test. The
logging opt-out header is requested; it is not a claim of guaranteed zero retention.
Health scenarios remain blocked until that probe and explicit acceptance for real data.

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

Official references: [SDK endpoint/project example](https://aistudio.yandex.ru/ru/docs/ai-studio/operations/generation/multimodels-request-responses),
[Responses API](https://aistudio.yandex.ru/ru/docs/ai-studio/api/Responses/createResponse),
[authentication and required roles](https://aistudio.yandex.ru/ru/docs/ai-studio/api-ref/authentication),
[logging-header caveat](https://aistudio.yandex.ru/docs/en/ai-studio/sdk-ref/sync/sdk.html),
and [model URI announcement](https://yandex.cloud/en/blog/digest-april-2026).
