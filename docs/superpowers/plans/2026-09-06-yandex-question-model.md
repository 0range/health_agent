# Separate Yandex question model settings

Keep the existing Yandex adapter boundary and add only two question-specific
settings. `YANDEX_QUESTION_MODEL` is optional and falls back to `YANDEX_MODEL`;
its model URI uses the same validated folder/model components. The question
client alone uses `YANDEX_QUESTION_TIMEOUT_SECONDS` (default 30, range 1–60).
Lab extraction continues using `YANDEX_MODEL`, timeout 30, zero retries, and the
existing consent/key/logging behavior. Synthetic tests cover fallback,
independent overrides, invalid components/ranges, and denial before client use.
