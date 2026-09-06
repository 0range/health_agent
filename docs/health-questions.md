# Health questions

TL;DR: ask through the CLI or a bound private Telegram chat. Answers use one
profile's verified labs, WHOOP records and bounded health snapshot, plus separately
attributed clinical-document excerpts and saved visit answers. Imported does not
mean verified, and this is not a complete search of the medical archive. It is an
information aid, not diagnosis, treatment, or emergency care.

## Local use

Start the local database and apply its schema first. Choose the permitted provider:
`AI_PROVIDER=openai` (the default) or `AI_PROVIDER=yandex`. For OpenAI, configure
the key only in the current shell or a private regular file
(`.tokens/openai-api-key` by default, mode `0600`, no symlink). For Yandex, follow
the [Yandex AI Studio guide](yandex-ai.md): it requires its own key, folder and an
explicit profile allowlist, empty by default. A configured bot does not grant
permission to share that profile's health data with a cloud provider.

Then ask one profile-scoped question:

```bash
uv run health-agent question ask --profile-id PROFILE_UUID "How has my sleep been?"
uv run health-agent question status --profile-id PROFILE_UUID
```

`question status` and Telegram `/status` count the capped recent observation list,
not all verified archive rows or the separate snapshot/reports. Thus `lab=0` can
coexist with dated historical labs available to an answer through the snapshot.

The service validates internal labels such as `[LAB1]`, `[SLEEP1]`, `[SNAP1]`,
`[DOC1]` and `[VISIT1]` against the selected evidence, then removes them from the
normal user-facing answer. It does not append a source list by default; the answer
keeps useful dates and values inline. Unknown labels and uncited generated answers
fail closed. Missing or insufficient evidence produces a compact meaningful
limitation, not invented measurements. Obvious urgent
symptoms receive immediate local emergency guidance before any provider request.

## Telegram

Configure and bind the bot only for the intended local profile, then start the
long poller:

```bash
uv run health-agent telegram configure-token
uv run health-agent telegram discover-id
uv run health-agent telegram bind PROFILE_UUID TELEGRAM_USER_ID
uv run health-agent telegram run
```

Only an explicitly bound, human, private Telegram chat reaches the question
service. `/status` is read-only; `/sync` only prints the existing Gmail/WHOOP
CLI commands and never starts synchronization. See the
[Telegram connector guide](integrations/telegram.md) for binding and token
storage details.

Prepared question and explicit medical-command replies are temporarily spooled under
`TELEGRAM_ROOT/prepared-replies` before delivery. Files contain the final reply
(including any displayed review candidate) and an opaque authentication-scope hash, never a
question, raw retrieval context, credentials, or dialogue history. The directory
is `0700`, files are regular `0600` files, names hash the bot/update identity,
and each reply is limited to 128 KiB. Atomic publication preserves the original
reply across a 429 retry, process restart, and multipart delivery. Previously
sent parts are skipped by the existing outbound hashes. After a committed
terminal update, the reply file is deleted; startup and incoming question/review handling sweep
orphan files older than seven days. An expired or manually removed spool cannot
guarantee replay; outbound hash conflicts and unknown-send fencing still fail
closed. No reply is used to answer a different question.

PDF/image imports and duplicate replays return the same canonical receipt and text,
allowing a deferred import acknowledgement to complete after restart.
JPEG/PNG originals use the shared vault with bounded local OCR. `/review` presents
one unverified item from this profile/bot/chat; only `/confirm UUID`,
`/correct UUID VALUE UNIT`, or `/reject UUID` changes review state. Corrected
values retain immutable source lineage. Unreviewed lab candidates are not patient
measurements in question context; clinical excerpts and saved visit answers use a
separate, unverified reported-material channel. Manual weight is intentionally
absent: WHOOP is the v0.1 source.

## Data boundaries and privacy

Retrieval is read-only and profile-scoped. The provider receives bounded selected
question/evidence text, including the clinical excerpts and saved visit answers
described below. It does not receive original PDFs/images, filenames,
raw WHOOP payloads, external account identifiers or other profiles'
records. Source pointers include local document/page and visit/note identifiers.
Questions, evidence, answers and keys are not printed by status commands or
operational errors.

The observation list uses 30 days for general/current-weight questions, 14 days
for sleep/recovery and 90 days for explicit weight-change questions, capped at
10 items per source. Both temporal bounds are inclusive: labs use their collected/issued
calendar date, while WHOOP uses observation time (body snapshots use sync-as-of
time). Old and future body records are excluded from this windowed list. The exact
UTC interval, source cap and time semantics are passed to the model. Internal
citations are validated before removal from normal display; relevant facts, dates
and compact inference-blocking limitations remain inline. Requesting a longer period
does not expand these windows. A body snapshot cannot establish weight change.
Mixed sleep/weight-change questions can answer supported sleep findings while
retaining that limitation.

The separate health snapshot has its own dates: latest verified labs per analyte
and source unit may predate the observation window, missing lab dates remain
unknown, wearable comparisons use the recent seven days against the preceding
28 days, and weight is a sync-time snapshot, not a measured trend. Presentation
selects at most 30 snapshot signals and prioritizes at most five attention items.
Old findings must not be described as today's condition; a gap is not a normal result.

`reported_material` contains at most five clinical-document excerpts and five saved
visit answers, each capped at 1,400 characters. Document selection examines at most
60 qualifying pages, takes one anchored section per document and excludes known
original-integrity errors and future medical dates. Visit answers exclude cancelled
visits and future-created notes. These channels use their own medical/recorded dates,
not the observation window; archive/import time is never a medical event date.
Reports are attributed wording or saved user notes, not verified measurements or
established diagnoses. This bounded selection is not exhaustive archive retrieval.

The OpenAI adapter makes a stateless Responses request with `store=False`, a hashed
profile safety identifier and no conversation chaining. Yandex uses native Chat
Completions, `reasoning_effort=none`, temperature zero, `store=False` and a logging
opt-out header; it omits the unsupported Responses safety-identifier field. Its
profile allowlist is checked before sending. These provider options are requests,
not guarantees of zero retention; see [Yandex setup and consent](yandex-ai.md).

`OPENAI_MAX_OUTPUT_TOKENS` defaults to 2,000 and allows 64–8,000 tokens;
`OPENAI_REASONING_EFFORT` defaults to `low` for the configured `gpt-5-mini`.
Both adapters default to a 30-second timeout and use no automatic retries. Yandex
question answering alone may be set from 1–60 seconds; its lab extractor remains
at 30 seconds. For OpenAI the token cap includes reasoning tokens, so incomplete
results still return unavailable; this policy has not been calibrated against
live completion rates. Changing models requires checking the model's supported
reasoning settings.

The hashed Telegram delivery ID also passes through the application to the
official `X-Client-Request-Id` tracing header. This is **not** a claim of provider
idempotency: the installed SDK's `extra_headers` option supports custom headers,
but neither its Responses signature nor the official Responses documentation
establishes a deterministic replay contract. Exact bytes come from the local
reply spool. See the official [request-ID documentation](https://developers.openai.com/api/reference/overview#supplying-your-own-request-id-with-x-client-request-id)
and [reasoning-token guidance](https://developers.openai.com/api/docs/guides/reasoning#controlling-costs).

Using either responder intentionally sends selected question and health evidence
to that provider; obtain the relevant profile owner's permission first. Yandex
enforces `YANDEX_ALLOWED_PROFILE_IDS` separately from provider selection. Cloud
lab extraction additionally requires its own explicit `--cloud` opt-in and budget;
enabling question answering does not approve imported lab candidates. Offline
tests use fake clients and synthetic disposable data, not real credentials,
Telegram accounts, OAuth or personal health data.

Implementation follows the official [Responses create reference](https://platform.openai.com/docs/api-reference/responses/create)
and [API safety best practices](https://developers.openai.com/api/docs/guides/safety-best-practices).

## Live-only validation

Before relying on this feature, separately validate the intended bot binding and
delivery and the selected, permitted provider using non-sensitive input. The
commands above perform real operations when configured; offline tests do not
establish live authorization, medical accuracy or complete archive coverage.
