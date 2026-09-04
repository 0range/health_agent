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

## Data boundaries and privacy

Every retrieval predicate includes the bound `profile_id`. The question context
is read-only and limited to verified labs and normalized WHOOP sleep, recovery,
cycle, workout, and current-weight values within fixed windows; it excludes raw
payloads, document text and filenames, external account identifiers, and other
profiles’ records. The production responder makes one stateless Responses API
request with `store=False`, a one-way profile safety identifier, bounded output,
and no conversation or response chaining. Questions, evidence, answers, and
keys are not printed by status commands or operational errors.

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
