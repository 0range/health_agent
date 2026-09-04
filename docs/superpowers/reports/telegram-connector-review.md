# Telegram connector review

Review target: `codex/v1-telegram` at `229ec6b`, relative to `305b837`.

## Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

This is an honestly scoped connector foundation, not a composed medical agent, and
its inbound profile boundary is sound in the ordinary single-process path. It is
not yet safe to use as an unattended local service: findings 1–5 can duplicate
outbound medical messages/downstream work, spin on failures, suppress one
profile's delivery, or discard a replacement bot's pending updates.

## Findings

### 1. High — retrying `sendMessage` after ambiguous transport failures can send duplicate medical messages

Every Bot API method shares `_post()`, which retries any `httpx.TransportError` and
all `5xx` responses (`src/health_agent/telegram/api.py:143-170`). That is correct
for reads such as `getUpdates` and `getFile`, but not for `sendMessage`: a timeout or
connection loss can occur after Telegram accepted the message but before the client
received the response. Retrying then sends the same reply/reminder again because
Telegram accepts no application idempotency key. The SQLite reservation surrounds
the whole gateway call and cannot distinguish or suppress retries inside it
(`src/health_agent/telegram/messenger.py:43-59`).

The integration guide and implementation report explicitly claim conservative
at-most-once delivery in the ambiguous crash window. Current network behavior is
instead potentially at-least-once, with up to four duplicate sends. Give mutating
methods a separate policy: retry only failures that prove rejection (notably 429),
or persist an explicit `delivery_unknown` state and require reconciliation. Add a
test where `sendMessage` acceptance is followed by a read-side transport failure.

### 2. High — a retryable update creates an unbounded hot loop against the agent and Bot API

`process_update()` records a transient downstream failure as `retryable_error` and
returns non-terminal (`src/health_agent/telegram/service.py:129-133`). The poller
breaks and returns normally (`:256-268`), so `run_forever()` does not enter its only
five-second sleep, which applies solely to a raised `TelegramTransientError`
(`:270-275`). On the next iteration, `begin_update()` reclaims every
`retryable_error` immediately, with no attempt count or next-retry timestamp
(`src/health_agent/telegram/stores.py:247-263`).

A temporarily unavailable question service can consequently trigger an unlimited
tight loop of `getWebhookInfo`, `getUpdates`, and model/agent calls for the same
private question. A retrying `/sync` adapter can likewise be invoked repeatedly.
Persist bounded attempts and `retry_at`, back off in `run_forever()` when a poll is
blocked, and terminally surface an action only after the autonomous retry budget is
exhausted. The existing test proves the offset stays put but stops before exercising
the loop (`tests/telegram/test_service.py:364-377`).

### 3. High — expiring claims are not owner/version guarded and can execute one update concurrently twice

There is no single-poller process lock. A `processing` row becomes reclaimable
after five minutes based only on `received_at`; `begin_update()` returns no claim
token, and `complete_update()` unconditionally updates by `update_id`
(`src/health_agent/telegram/stores.py:216-277`). A legitimate long model request,
source sync, transcription, or large attachment import can exceed that lease. A
second poller can then reclaim and run the same operation while the first remains
active, and either stale worker can overwrite the other's terminal status.

The inbox is required to deduplicate its final commit, and outbound reply keys
suppress many repeats, but `HealthCommandService.sync()` and
`HealthQuestionService.answer()` have no idempotency/lease contract. Side effects,
model cost, and sync work can therefore duplicate. Use a single-instance lock or
owner+generation claims with lease renewal and compare-and-set completion. Test a
slow worker crossing the lease while a second process polls.

### 4. High — outbound idempotency keys are global rather than profile scoped

`outbound_audit` has primary key `(delivery_key, part_index)`; `profile_id` and
`chat_id` are ordinary columns (`src/health_agent/telegram/stores.py:127-138`).
`reserve_outbound()` silently treats any existing key as already delivered without
checking that its stored profile/chat matches the current request (`:322-337`).
`TelegramMessenger` passes caller-provided reminder keys unchanged
(`src/health_agent/telegram/messenger.py:26-48`).

If two profiles independently use a natural key such as `checkup:2027`, the first
profile reserves it and the second profile's reminder is silently suppressed. A
reused key with changed chat or text is also reported as previously reserved. This
violates the mandatory profile boundary even though it does not disclose one
profile's content to another. Make the primary identity include `profile_id` (and
validate chat/content revision semantics), while keeping reply update IDs globally
safe. Add a two-profile proactive-delivery test using the same business key.

### 5. High — replacing the token with a different bot reuses its predecessor's offset and idempotency namespace

The token store can be overwritten without calling `getMe`, while the singleton
SQLite runtime has no stored bot ID and keys inbound updates globally by
`update_id` (`src/health_agent/telegram/admin.py:49-50`;
`src/health_agent/telegram/stores.py:101-119`). If the owner pastes a token for a
different BotFather bot, the next `getUpdates` sends the previous bot's
`next_offset`. Telegram treats a higher offset as confirmation, so this can discard
pending updates belonging to the new bot before they are processed. Colliding
`update_id` and outbound delivery keys can also make new-bot work look terminal or
already reserved.

Verify `getMe` before publishing a token, durably bind state to that bot's numeric
ID, and refuse an identity change until the owner explicitly performs a safe reset
or creates a separate bot namespace. This is both a data-loss and wrong-status path
that the current `token_configured` existence check cannot reveal.

### 6. Medium — Telegram's `retry_after` is capped and therefore not honored

The Bot API defines `retry_after` as the seconds remaining before a request may be
repeated. `_wait_for_data()` caps every value at 30 seconds
(`src/health_agent/telegram/api.py:183-191`). For a larger flood-control delay the
connector retries early four times and fails instead of waiting until allowed. The
download path also sleeps after a 429 on the final attempt before failing
(`:108-135`). Honor the returned value or persist a deferred retry time and exit
cleanly; do not silently shorten it. The current test covers only `retry_after=3`.

Official semantics:
<https://core.telegram.org/bots/api#responseparameters>

### 7. Medium — bind prechecks and the SQLite write are not one atomic identity operation

`TelegramAdminService.bind_identity()` checks user and profile ownership through
separate SQLite connections, then calls `bind_identity()` through another
(`src/health_agent/telegram/admin.py:52-75`). The store uses
`ON CONFLICT(telegram_user_id) DO UPDATE` but deliberately does not update or assert
the stored `profile_id` (`src/health_agent/telegram/stores.py:143-167`). Two panel
or CLI requests can both observe no binding; one inserts user→profile A and the
other takes the conflict path, returns a successful profile-B identity to its
caller, yet leaves A in the database. The inverse race on the unique profile can
leak a raw `sqlite3.IntegrityError` rather than `TelegramIdentityConflict`.

Move conflict validation and mutation into one immediate transaction and return the
actual committed row. The database still retains a one-to-one mapping, but current
service results/status can lie under exactly the concurrent management-panel use
for which the service is advertised.

### 8. Medium — filesystem hardening omits directory and SQLite symlink checks

Token loads reject a final-file symlink and successful token/state files are
chmodded `0600`, with their directory chmodded `0700`. However,
`_private_directory()` follows an existing symlink and chmods its target without
`lstat` validation (`src/health_agent/telegram/stores.py:19-22`). More importantly,
`SqliteTelegramState` passes its configured path straight to `sqlite3.connect`, so
a symlinked `state.sqlite3` is followed and opened/modified before the later chmod
(`:74-88`). The configured token's parent directory has the same redirection gap.

For the agreed single-user Mac threat model this is not a remote exploit, but the
task explicitly promises symlink-safe private state. Reject symlinks/non-directories
at every existing path component and reject a symlink/non-regular SQLite target
before opening it. Add root/profile/file symlink tests.

### 9. Medium — attachment MIME and post-ingest validation are not a committed safety boundary

The gateway and `_AuditedChunks` do enforce an actual 20 MiB stream bound and an
independent SHA-256, but `mime_type` is copied from Telegram message metadata (or
filename-independent defaults) without signature sniffing
(`src/health_agent/telegram/service.py:292-350`). It must be treated as untrusted by
the future medical inbox; the protocol/docs do not state that explicitly.

The independent digest, complete-consumption check, receipt size/hash, and Telegram
size comparison run only after `MedicalInbox.ingest()` returns
(`:165-187,353-401`). An inbox that has already committed and returns a wrong
receipt—or a metadata mismatch discovered at this stage—leaves its side effect but
records no attachment audit; the broad handler then marks the update terminal
`needs_attention`. Stage and validate bytes before the medical commit, or strengthen
the inbox transaction/idempotency contract and record the resulting document plus
validation failure durably. When the actual stream exceeds the limit despite
missing/incorrect metadata, the current API-error path also terminates without the
specific user-facing size reply used by the preflight path.

### 10. Medium — status checks path presence, not a usable bot or running service

`TelegramAdminService.status()` sets `token_configured` from `exists()` only
(`src/health_agent/telegram/admin.py:81-93`), and `exists()` does not parse the token
or call `getMe` (`src/health_agent/telegram/stores.py:70-71`). A malformed,
unreadable, revoked, or wrong-bot token can therefore show configured. Status also
cannot say whether a poller/composition service is installed or alive.

In addition, the `getWebhookInfo` call in `poll_once()` is outside the exception
block that persists poll errors (`src/health_agent/telegram/service.py:241-255`), so
its transport/auth failure is absent from `last_error_code`; a permanent API error
then exits `run_forever()`, which catches transient errors only. Keep
`configured` distinct from `verified` and `running`, persist every safe poll
failure, and expose freshness/heartbeat suitable for the management-panel card.

### 11. Low — malformed Bot API objects can terminate the service outside its safe lifecycle

The gateway validates only that each update is a dictionary. Sorting converts its
`update_id` before the processing error boundary; invalid message IDs and several
`int(...)` response conversions can likewise raise plain exceptions
(`src/health_agent/telegram/service.py:246-252,70-84`;
`src/health_agent/telegram/api.py:88-98,137-141,166-168`). `run_forever()` catches
only `TelegramTransientError`, so a malformed but successful upstream response can
stop the service without a durable safe status. Telegram normally satisfies its
schema, making this low probability, but unattended operation should validate and
quarantine the one bad update rather than halt all later ones.

## Confirmed behavior

- Current official Bot API semantics match the implementation's HTTPS endpoints,
  long-poll `getUpdates`, `limit=100`, positive timeout, explicit `message`
  allowlist, webhook mutual exclusion, offset=`update_id+1`, 24-hour retention,
  `getFile` URL and 20 MiB cloud-download limit, `sendMessage` 1–4096-character
  limit, and response-body `retry_after`. `allowed_updates` not applying to already
  queued updates is safe because unsupported updates are terminally ignored.
- Updates are sorted and processed in order. The local offset advances only after
  each terminal result and uses a monotonic SQLite update. A crash after terminal
  completion but before offset advancement safely replays the audit result without
  rerunning downstream work.
- Unknown users, bot senders, groups/channels, wrong private-chat IDs, and valid-ID
  update types without a supported message receive no reply and invoke no agent,
  command, or inbox service. Their minimal numeric operational metadata is retained
  locally; no question or file body is stored.
- In the normal sequential path, one active Telegram user ID maps to one Profile
  UUID and one profile maps to one user. Every downstream context and attachment
  carries that UUID, identical bytes for two profiles keep separate source
  occurrences, and incoming reply keys use Bot API update IDs that are unique
  within one fixed bot identity.
- Downloads use `httpx.stream`, stop retrying once any body byte was emitted, cap
  both gateway and audited consumption, and calculate an independent full-stream
  SHA-256. Nested inbox code cannot consume the one-pass stream twice.
- Successful token writes use a temporary file, fsync, atomic replace, and `0600`;
  SQLite and containing directories are also chmodded private. Tokens are prompted
  without shell arguments, excluded from Git by default, redacted from explicit
  API exceptions, and absent from audit tables.
- Audit tables omit incoming question/caption text, reply bodies, attachment bytes,
  filenames, and MIME. They retain local IDs, profile, kind/status, hash/size, a
  caller-provided external reference, and delivery keys. Those caller-provided
  strings should remain opaque/non-medical.
- The focused Telegram tests use `tmp_path`, fake services and mocked HTTP. They do
  not call a real bot, cloud model, medical inbox, or live health database. The
  full-suite PostgreSQL gate in the implementation report is the pre-existing
  disposable fixture, not Telegram runtime coupling. No migration was added.

Official sources checked on 2026-09-04:

- <https://core.telegram.org/bots/api#getupdates>
- <https://core.telegram.org/bots/api#getfile>
- <https://core.telegram.org/bots/api#sendmessage>
- <https://core.telegram.org/bots/api#responseparameters>
- <https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this>

## Honest composition boundary and live-only assumptions

- The branch intentionally supplies protocols/router/gateway/state/admin services,
  not concrete `HealthQuestionService`, `HealthCommandService`, `MedicalInbox`, a
  poller CLI command, launchd job, or management page. README, integration guide,
  and implementation report disclose this accurately. Their absence is not counted
  as a connector-foundation defect, and no fake medical answer is present.
- Consequently, no real user scenario—health question grounded in personal data,
  Telegram file through the medical importer, `/status`, `/sync`, reminder, or Mac
  restart—has passed end to end. The complete v1 readiness criteria remain unmet
  until composition and live acceptance exist.
- BotFather creation, token validation via `getMe`, private `/start`, numeric-ID
  discovery, webhook state, a real long-poll response, file download, 429, and
  outbound delivery have not been exercised with Telegram.
- The future composition root must preserve opaque delivery keys, inbox
  profile/source idempotency, untrusted-MIME handling, bounded processing time or
  lease renewal, and one active poller per bot. It must not present this foundation's
  local `configured` flag as a connected/running status.

## Review method

Read the approved v1, multi-profile, autonomy, and management-panel design; the
connector plan, implementation report, integration guide, branch diff, current
source, and tests. Checked protocol details against Telegram's current official Bot
API and Bot FAQ. Per instruction, this review changed no implementation and did not
rerun tests; reported gate results are implementation-report evidence only.
