# Health questions

The local Health Agent can answer a free-form health question from a selected
profile’s verified lab results and normalized WHOOP records. It is an
information aid, not diagnosis, treatment, or emergency care.

## Local use

Start the local database and apply its schema first. Configure an OpenAI API key
only in the current shell or in a private regular file (`.tokens/openai-api-key`
by default, mode `0600`, no symlink). Then ask one profile-scoped question:

```bash
uv run health-agent question ask --profile-id PROFILE_UUID "How has my sleep been?"
uv run health-agent question status --profile-id PROFILE_UUID
```

The answer has a deterministic `Sources:` footer. Evidence labels such as
`[LAB1]` and `[SLEEP1]` map only to the bounded, verified observations selected
for that request. An empty or insufficient source window produces an explicit
insufficient-evidence answer. Obvious urgent symptoms receive immediate local
emergency guidance before an OpenAI request.

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

Prepared question replies are temporarily spooled under
`TELEGRAM_ROOT/prepared-replies` before delivery. Files contain the final reply
(including its Sources footer) and an opaque authentication-scope hash, never a
question, raw retrieval context, credentials, or dialogue history. The directory
is `0700`, files are regular `0600` files, names hash the bot/update identity,
and each reply is limited to 128 KiB. Atomic publication preserves the original
reply across a 429 retry, process restart, and multipart delivery. Previously
sent parts are skipped by the existing outbound hashes. After a committed
terminal update, the reply file is deleted; startup and incoming questions sweep
orphan files older than seven days. An expired or manually removed spool cannot
guarantee replay; outbound hash conflicts and unknown-send fencing still fail
closed. No reply is used to answer a different question.

PDF imports and duplicate replays return the same canonical receipt and text,
allowing a deferred import acknowledgement to complete after restart.

## Data boundaries and privacy

Every retrieval predicate includes the bound `profile_id`. The question context
is read-only and limited to verified labs and normalized WHOOP sleep, recovery,
cycle, workout, and current-weight values within fixed windows; it excludes raw
payloads, document text and filenames, external account identifiers, and other
profiles’ records. The production responder makes one stateless Responses API
request with `store=False`, a one-way profile safety identifier, bounded output,
and no conversation or response chaining. Questions, evidence, answers, and
keys are not printed by status commands or operational errors.

General and current-weight questions select 30 days; sleep/recovery selects 14
days; explicit weight-change questions select 90 days. Each source is capped at
10 items. Both temporal bounds are inclusive: labs use their collected/issued
calendar date, while WHOOP uses observation time (body snapshots use sync-as-of
time). Old and future body snapshots are excluded. The exact selected UTC
interval, source cap, and time semantics are passed to the model and shown in
the deterministic Sources footer even when the question requests a longer
period. A body snapshot cannot establish weight change. Mixed sleep/weight-change
questions can answer the supported sleep portion while retaining that limitation.

`OPENAI_MAX_OUTPUT_TOKENS` defaults to 2,000 and allows 64–8,000 tokens;
`OPENAI_REASONING_EFFORT` defaults to `low` for the configured `gpt-5-mini`.
The SDK uses a 30-second timeout and no automatic retries. The token cap includes
reasoning tokens, so incomplete results still return unavailable; this policy
has not been calibrated against live completion rates. Changing models requires
checking the model's supported reasoning settings.

The hashed Telegram delivery ID also passes through the application to the
official `X-Client-Request-Id` tracing header. This is **not** a claim of provider
idempotency: the installed SDK's `extra_headers` option supports custom headers,
but neither its Responses signature nor the official Responses documentation
establishes a deterministic replay contract. Exact bytes come from the local
reply spool. See the official [request-ID documentation](https://developers.openai.com/api/reference/overview#supplying-your-own-request-id-with-x-client-request-id)
and [reasoning-token guidance](https://developers.openai.com/api/docs/guides/reasoning#controlling-costs).

Enabling this responder intentionally sends the bounded selected question and
evidence to OpenAI. Do not enable it until that external processing is
appropriate for the profile. The offline test suite uses a fake Responses client
and synthetic disposable data; it does not use a real key, Telegram bot, OAuth,
or personal health data.

Implementation follows the official [Responses create reference](https://platform.openai.com/docs/api-reference/responses/create)
and [API safety best practices](https://developers.openai.com/api/docs/guides/safety-best-practices).

## Live-only validation

Before relying on this feature, the owner must separately validate a real
BotFather token, a bound private-chat update and delivery, and a permitted
OpenAI account/key with non-sensitive test data. That validation is deliberately
not performed by this repository’s tests or by the commands above.
