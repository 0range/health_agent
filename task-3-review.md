# Task 3 Independent Review

Scope reviewed: approved question-loop design and implementation plan, Task 3
report, commit range `3d570d8..99f51c2`, and the current Telegram/CLI
contracts. No tests were run and no production files were changed.

## Verdict

**SPEC: FAIL.** The checked-in `telegram run` composition is not a complete
production composition, does not use an existing medical inbox, and gives an
invalid Gmail command in `/sync`.

**QUALITY: FAIL.** Safe status and run-time failure behavior has uncovered
paths, and status can report a responder ready when `question ask` cannot
construct it.

## SPEC findings

### S1 — HIGH: `telegram run` does not establish the required bot namespace

`build_telegram_question_runtime` loads a credential and creates
`SqliteTelegramState`, but never calls `state.register_bot(credential.bot_id,
credential.username)` ([composition.py:227](src/health_agent/questions/composition.py#L227)).
`register_bot` is what creates both the foreign-key parent in `bot_namespaces`
and its `runtimes` row ([stores.py:380](src/health_agent/telegram/stores.py#L380)).

Consequently, a valid verified credential file paired with a new/different
state file can make `telegram run` print `status=running`, then fail on the
first update claim because `updates.bot_id` references a missing namespace.
It also means no poll/status heartbeat is persisted for that namespace. The
only working setup is an implicit prior `telegram configure-token` against the
same state file; the production runtime itself is not self-sufficient as Task
3 requires. Register (or positively verify) the namespace during composition,
before making the poller available, and test a fresh state path.

### S2 — HIGH: the CLI's only attachment path is deliberately non-ingesting

The approved design and plan require the existing medical-inbox adapter
([design:32-34](docs/superpowers/specs/2026-09-04-health-question-loop-design.md#L32),
[plan:27-31](docs/superpowers/plans/2026-09-04-health-question-loop.md#L27)).
Instead, the sole path used by `telegram run` selects
`NeedsAttentionMedicalInbox()` whenever no programmatic caller supplies an
inbox ([composition.py:217-244](src/health_agent/questions/composition.py#L217)).
That fallback truthfully consumes the fully staged stream, hashes it, and
returns `needs_attention`/`not imported` ([composition.py:131-155](src/health_agent/questions/composition.py#L131)); those semantics are good for an
explicit no-ingestion mode. But the CLI cannot inject the promised real inbox,
so all live Telegram attachments are permanently not imported. A documented
limitation in the Task 3 report does not satisfy the approved composition
requirement. Supply a real Telegram `MedicalInbox` in the default composition,
or exclude attachments/`telegram run` from delivery until one exists.

### S3 — HIGH: `/sync` gives a non-existent Gmail invocation

`SYNC_INSTRUCTIONS` tells users to run `health-agent gmail sync --profile-id
<your-profile-id>` ([composition.py:45-48](src/health_agent/questions/composition.py#L45)).
The actual Gmail CLI declares `profile_id` as a required positional argument,
not `--profile-id` ([cli.py:496-500](src/health_agent/cli.py#L496)). The
existing documented form is `health-agent gmail sync PROFILE_UUID`; only the
WHOOP command accepts `--profile-id`. This violates the command's sole job:
accurate, read-only handoff guidance. Use e.g. `health-agent gmail sync
<your-profile-id>` and retain `health-agent whoop sync --profile-id
<your-profile-id>`.

## QUALITY findings

### Q1 — MEDIUM: `question status` has no safe boundary around `Settings()`

Unlike `question ask`, the status command constructs `Settings()` outside any
`try` ([cli.py:604-608](src/health_agent/cli.py#L604),
[cli.py:615-627](src/health_agent/cli.py#L615)). A configuration validation
failure can therefore escape Typer rather than return the specified safe
`status=unavailable ... error=...` result. Validation errors often render the
offending input, so this also leaves the status secret-free promise dependent
on framework exception formatting. Catch settings construction and emit a
stable safe error exactly as the other question entry points do.

### Q2 — MEDIUM: status readiness does not validate the responder that ask
uses

`question_status` checks only `load_openai_api_key()`
([composition.py:191-202](src/health_agent/questions/composition.py#L191)); it
does not construct/validate `OpenAIResponsesResponder`, while `ask` does
([composition.py:167-177](src/health_agent/questions/composition.py#L167)).
For example, an all-whitespace `OPENAI_MODEL` passes `Settings` and the status
key check, so a profile with readable context is reported ready although the
responder constructor rejects that model and every ask returns unavailable.
Share a no-network responder configuration/readiness constructor or validate
the same model/token settings in status.

### Q3 — MEDIUM: `telegram run` reports running before identity validation and
does not safely handle poller failures

The runtime only reads locally stored credential material; remote identity
validation happens later at the first `poll_once` ([composition.py:227-229](src/health_agent/questions/composition.py#L227),
[service.py:451-465](src/health_agent/telegram/service.py#L451)). The command
prints `status=running` before that point, catches only `KeyboardInterrupt`,
and lets any non-Telegram poller/state failure escape
([cli.py:653-665](src/health_agent/cli.py#L653)). In particular, the namespace
defect above is an uncaught runtime error. Also, a bot identity mismatch is
treated by `run_forever` as a generic Telegram API error and retried forever,
rather than being surfaced as a blocked startup condition. Validate identity
and namespace before announcing readiness, map unrecoverable run errors to a
stable secret-free terminal result, and keep the existing clean SIGINT exit.

## Confirmed properties

- Profile selection is correctly derived from the already authenticated,
  private `MessageContext`; no text-supplied profile identifier is accepted
  ([composition.py:101-104](src/health_agent/questions/composition.py#L101)).
- Database evidence lookup gets a short-lived, closed SQLAlchemy session per
  request ([composition.py:84-92](src/health_agent/questions/composition.py#L84)).
- The no-ingestion fallback fully consumes validated chunks and its receipt
  matches the staged byte count/hash, so it does not claim an import that did
  not occur.
- The composition keeps injectable factories for the engine/context/responder
  and Telegram external boundaries. Its current tests cover only injected
  composition, not the fresh-state production path.
- No existing claim fencing, Telegram update routing, or outbound idempotency
  implementation was weakened by this diff; the composition reuses
  `TelegramUpdateService` and `TelegramMessenger`. The missing namespace
  prevents those existing protections from being reached in an unbootstrapped
  state.

## Fix-round re-review — `99f51c2..67497de`

Scope: current implementation, Task 3 report, and the requested follow-up
checks. No tests were run and no production files were changed.

**SPEC: PASS.** The prior delivery requirements are now implemented: runtime
composition registers the verified bot before update-state foreign-key use,
startup validates `getMe` and webhook state before `status=running`, `/sync`
uses the actual profile-bound CLI forms, and profile existence is enforced at
the binding and question-context boundaries. The default inbox gates on the
signature-validated media type, consumes and hashes the complete staged stream,
uses the normal vault/importer transaction with Telegram provenance, and
returns importer-derived statuses. Existing update claims, delivery fencing,
and outbound idempotency remain unchanged.

**QUALITY: FAIL.** One interruption path can retain a raw transient PDF.

### Q4 — MEDIUM: temporary raw PDF is not cleaned up on `KeyboardInterrupt`

`TelegramMedicalInbox._write_private_copy` cleans its `mkstemp` file only under
`except Exception` ([composition.py:240-247](src/health_agent/questions/composition.py#L240)).
`KeyboardInterrupt` and other `BaseException` subclasses raised while iterating
or writing `chunks` bypass that handler; the outer `ingest` `finally` has not
yet been entered because `_write_private_copy` has not returned. Thus a user
interrupt during a Telegram attachment write leaves a private but raw PDF in
`temporary_root`, contrary to the required cleanup-on-failure property. Put
the unlink in a `finally` guarded by a successful-write flag (or catch
`BaseException`) so ordinary errors and interrupts remove the transient file.

## Fix-round confirmed properties

- `register_bot` occurs before the gateway, poller, or update-service can use
  the namespace; `register_bot` creates the matching runtime row.
- `validate_startup` checks bot identity and webhook configuration, records
  safe Telegram API failure codes, and the CLI exposes only stable blocked
  codes for startup or later poller failures.
- `question status` constructs the same local responder as `ask`, identifies
  that readiness as local, and safely maps settings/context failures.
- Validated PDFs are imported with `profile_id`, `source_provider="telegram"`,
  and the update-derived `source_external_id`; document SHA deduplication and
  source-record revision idempotence are retained. Non-PDFs are fully consumed
  and truthfully reported as not imported.
- Transient roots reject symlink components and regular completion/failure
  paths remove the copy. Persistent attachment bytes are held in the existing
  vault rather than a new raw store; Q4 is the remaining interruption cleanup
  exception.
