# Health Question Loop — whole-branch independent review

Reviewed range: `1387d5e..c0fd3bf`. Review date: September 5, 2026.

SPEC FAIL

QUALITY CHANGES

OVERALL NOT READY

The branch implements the principal architecture and resolves the defects recorded in
the task reviews. Whole-path inspection nevertheless finds two high-priority release
blockers and two medium-priority correctness gaps. These are static findings; no tests,
application commands, health-data reads, credential reads, or live API calls were run for
this review. Only this report was authored.

## Findings

### R1 — HIGH: deferred delivery recomputes content and permanently conflicts with its outbound reservation

The new question adapter calls the application afresh for every invocation
(`src/health_agent/questions/composition.py:117`). On Telegram deferral, the existing
update service schedules the update again, then reruns routing before sending with the
same `telegram-update:{update_id}:reply` key
(`src/health_agent/telegram/service.py:196`, `:353`). The messenger/state deliberately
reject a different content hash for that key and part
(`src/health_agent/telegram/stores.py:809`).

For example, OpenAI produces answer A, Telegram returns 429, and the next attempt produces
answer B. Even a wording-only difference becomes `outbound_idempotency_conflict`, which
the update service treats as terminal failure. If the first part was already sent, later
parts can be lost; if the first send was deferred, the user receives no answer. The new
PDF path has a deterministic version of the same defect: import succeeds but its reply
is deferred; the replay returns `duplicate`, changing the reply from “Medical PDF
imported…” to “This medical PDF was already imported.” That also conflicts with the
original reservation (`composition.py:232`, `:273`).

Action: retain the exact prepared reply and its parts for the delivery attempt, scoped
to the authenticated bot/profile/update, and resume delivery of those bytes without
rerunning generation or changing an import receipt. Define restart behavior explicitly
within the existing private-storage policy. Preserve content conflicts and unknown-send
at-most-once handling; weakening the hash comparison is not a fix. Add offline integration
coverage for a first-send 429 with a changing responder, a later-part 429, and an imported
PDF whose reply is deferred before its duplicate replay.

### R2 — HIGH: the informational-question exemption suppresses explicit emergencies

`src/health_agent/questions/safety.py:45` returns false before testing symptoms whenever
the message starts with a question word and lacks one of a few explicit first-person
pronouns. Russian commonly omits that pronoun. Thus `Что делать, не могу дышать?` and
`Как быть, хочу умереть?` bypass the guard even though their direct emergency phrases
are already present in `_URGENT_PATTERNS`. `What should we do? He cannot breathe.` has
the same problem. With evidence, these messages reach OpenAI; without it they get the
ordinary insufficient-evidence response. The remote instruction additionally says the
application has already handled obvious emergency language (`openai.py:33`).

Action: prioritize direct symptom/self-harm statements, including implicit Russian
first person and explicit third-person emergencies, before any generic-topic exemption.
Narrow the exemption to genuinely informational constructions. Add these examples to
safety and application tests and assert no retrieval or responder call. Keep the existing
“What causes chest pain?” and ordinary blood-pressure negative cases.

### R3 — MEDIUM: retrieval-window intent is incorrectly used as a universal weight-insufficiency decision

`_WEIGHT_TREND_TERMS` includes generic `trend`, `динамик`, and `тренд`, and
`_limitations_for()` treats every resulting intent as an unanswerable weight change
(`src/health_agent/questions/context.py:57`, `:316`, `:353`). Consequently “Show my
ferritin trend” or “Покажи динамику холестерина” receives a local weight-history refusal
even when two verified lab observations are present. “What is my current weight?” also
gets a trend refusal despite a current snapshot being available.

The reverse failure occurs for mixed questions: sleep/recovery matching takes precedence,
so “How have my sleep and weight changed?” gets no weight-history limitation at all.
It makes a remote call despite the intended deterministic insufficiency contract.

Action: separate window selection from the requested inference. Apply weight-history
insufficiency only to a request for weight change, preserve it in mixed-topic questions,
and allow other laboratory trends and current-snapshot questions through their appropriate
paths. Add all three categories to the context/service tests.

### R4 — MEDIUM: the advertised bounded evidence window is not maintained through retrieval and presentation

`_weights()` accepts `window_start/window_end` but applies neither bound
(`src/health_agent/questions/context.py:272`). A stale or future-dated current-body row is
included even for an ordinary general or sleep question. The existing stale-body test
explicitly expects a 180-day-old snapshot in a 90-day context. Correctly naming its time
as synchronization time fixes measurement provenance, but does not make the design and
guide's claim that all supplied values are within fixed windows true.

Furthermore, the responder input omits `window_start`, `window_end`, and the typed
`time_semantics`; every date is serialized as `observed_on`
(`src/health_agent/questions/openai.py:114`, `:147`). The footer lists item dates but does
not disclose the selected interval or source caps (`service.py:140`). The model and user
therefore cannot reliably distinguish the chosen interval from missing history when a
free-form question asks for a longer period.

Action: enforce the documented temporal policy, including rejecting future sync dates.
If stale current snapshots are an intentional exception, make that explicit in typed
context, the prompt, the local footer, and the design/guide. Send the actual selected
window and observation-versus-sync semantics, and disclose bounded selection in the
answer. Add stale/future snapshot and requested-period-versus-selected-period coverage.

## Spec compliance assessment

The SQL is parameterized and profile-scoped. Laboratory ownership is established by its
document join and only verified observations qualify. Recovery links use both profile
and connection identity and unique normalized source keys; physiology dates come from
sleep/cycle starts rather than record-update times. The default database wrapper validates
profile existence, materializes plain evidence values inside a short-lived session, and
closes that session before the responder is called. No detached ORM access or cross-profile
join was found. R3 and R4 are the remaining inference/window failures.

The model receives bounded JSON blocks with separate instructions, no raw records,
document text, source identifiers, or profile UUID. The application owns the source
footer and rejects missing, unknown, malformed, nested, and oversized bracket labels.
Those controls prevent the concrete section/citation parsing defects previously reported;
they do not prove semantic truth of an answer that uses a valid label. Medical uncertainty
and no-diagnosis/no-treatment instructions are present. R2 prevents approval of the
required local urgent path.

`question ask`, count-only local `question status`, and `telegram run` are implemented.
Production composition loads a verified bot credential, registers its state namespace,
uses the authenticated private binding, and validates remote bot identity/webhook state
before printing running. `/sync` supplies the actual bound-profile connector commands.
The default attachment adapter uses staged, signature-validated PDFs, private transient
files, the existing vault/importer transaction, and profile/source provenance; non-PDFs
truthfully require attention. Normal and interrupted writes remove the transient copy.
R1 prevents approval of the assembled retry behavior despite unchanged transport fencing.

## Code quality and verification assessment

The implementation has useful injectable boundaries and closed user-facing error codes.
Question/status/runtime error paths suppress exception details. No new operational logging
of keys, questions, evidence, answers, or raw files was found. The OpenAI environment key
takes precedence; fallback loading checks the opened descriptor for regular-file/exact
0600 mode and rejects a final symlink. Local responder readiness is explicitly labelled
local and does not claim remote account readiness. The SDK receives the configured key;
Telegram startup verifies the remote identity against the saved bot ID.

The official SDK call is `responses.create`, with `store=False`, capped output, a hashed
safety identifier, no conversation/chaining, and a completed-status/nonempty-`output_text`
check. Local SDK source and official OpenAI documentation support those API semantics.
Refusals/empty output and incomplete results safely become unavailable rather than exposing
partial medical output.

The reported 393-test, Ruff, mypy, and disposable-database Alembic results are prior-run
evidence, not rerun or independently reproduced here. The new integration test really
uses PostgreSQL retrieval and real SQLite/update/messenger/poller components with fake
OpenAI/Telegram transports and other-profile sentinels. It covers one immediate successful
reply, not R1's retry cases. Configuration and CLI tests often replace production factories;
their names must not be read as live/default-composition validation. The database fixture
uses local Docker/TCP and may fetch its image if absent, so “offline” means no live health
service calls, not an enforced absence of all network activity.

No migration file changes appear in this branch. The only WHOOP model addition is a Python
property; the existing linear lineage still ends at `0005_whoop`, and the recorded
`command.check` gate checks metadata on a disposable database. The Gmail test edits add
explicit optional-value assertions without changing their behavioral intent. The mypy
exclusions are documented. The supplied design, plan, final report, Task 1/2 reports and
complete review histories, Task 3 report/review history, whole-branch review package,
changed implementation/tests/docs, and relevant existing contracts were inspected.

## Live-only and operational concerns

- Real BotFather identity/private delivery, permitted OpenAI credentials/model access,
  and the owner's external health-data processing decision remain unvalidated, as the
  delivery report correctly states.
- The default `gpt-5-mini` is a reasoning model. Its 400-token budget (hard maximum 1,000)
  includes hidden reasoning, so a valid request can finish incomplete before any visible
  answer. The completed-status guard is correct, but fake completed responses do not
  validate useful success rates. Measure synthetic live completion rates and tune the
  model/reasoning/budget policy before relying on the default. This is a documented risk,
  not a claim that a particular live request failed. See the official
  [GPT-5 Mini reference](https://developers.openai.com/api/docs/models/gpt-5-mini) and
  [reasoning token guidance](https://developers.openai.com/api/docs/guides/reasoning#controlling-costs).
- The SDK factory uses default timeout/retry settings (the installed SDK has a 600-second
  timeout and two retries). Because update processing is serial, one stalled request can
  delay subsequent updates substantially, including urgent ones. Establish an appropriate
  application latency budget and test bounded failure before unattended operation.
- Pure prompting plus valid-label checking does not establish clinical accuracy or
  resistance to every semantic injection. Live evaluation must include adversarial
  questions and unsupported claims, using synthetic data. No clinical validation is claimed.

Resolve R1–R4 and add the focused offline regression evidence before another whole-path
approval. Live validation remains a separate owner-operated step.

## Final fix-round re-review — `b3a9c0b..2377029`

Reviewed September 5, 2026 against the pinned fix range, `final-fix-review.diff`,
the updated implementation report/design/guide, current changed code and tests,
and the existing state/importer contracts. This section supersedes the original
verdict for the reviewed range. No tests or application commands were run and
no implementation was edited.

SPEC FAIL

QUALITY CHANGES

OVERALL NOT READY

R2, R3, and R4 are resolved for the reviewed requirements. R1's free-form question
path and normal imported-PDF path are fixed, but the other successful PDF importer
outcomes still break replay. One new cleanup race also needs correction.

### F1 — HIGH: OCR-required/needs-attention PDF receipts still change on duplicate replay

`src/health_agent/questions/composition.py:250` maps only `duplicate` to
`imported`; it preserves the first import's `ocr_required` or `needs_attention`
status. The first response for those statuses also differs from the duplicate
response. Both statuses are ordinary successful persistence outcomes of the
existing `import_document` pipeline (`src/health_agent/importer.py:201`).

Concrete sequence: a scanned PDF is persisted with `ocr_required`; attachment
audit records that receipt; its reply gets a 429; replay finds the existing
document and returns `duplicate`, which the adapter converts to `imported`.
`SqliteTelegramState.record_attachment` compares the complete saved receipt
tuple, including status, and returns false. The update exits as `claim_lost`
before resending. Its processing lease eventually expires, but each subsequent
replay repeats the mismatch. This path does not use `_defer`'s retry-budget
termination, so it can hold up later ordered updates indefinitely.

Action: make the transport receipt and acknowledgement stable for every
successfully persisted PDF outcome, including `ocr_required`, `needs_attention`,
`imported`, and their duplicate replays. A single truthful “received; processing
or review may be required” acknowledgement is sufficient; keep the original
processing state in the document and preserve the document ID/hash/size.
Parameterize the first-send deferral/restart test across those initial outcomes,
then assert identical audit tuples/text and terminal completion. Do not weaken
attachment-audit or outbound conflict checks.

### F2 — MEDIUM: normal concurrent cleanup can abort an unrelated question or runtime startup

`PrivateReplyStore.sweep` enumerates names, then calls `lstat()` and `unlink()`
without tolerating disappearance (`src/health_agent/questions/replies.py:115`).
A second worker can publish and unlink a `.reply-*` temporary file, or complete
another update and remove its final reply, after enumeration but before `lstat`.
Two sweepers can also both observe an expired file before one deletes it.
The resulting `FileNotFoundError` escapes the sweep.

Because sweeping occurs in the store constructor and at the start of every
question, these benign races can block startup or turn a healthy request into
the generic terminal processing failure. They do not require malicious file
replacement or an invalid spool.

Action: tolerate only `FileNotFoundError` for entries that disappear during
inspection or deletion, then continue sweeping. Preserve rejection of symlinks,
invalid modes, and other actual filesystem failures. Add deterministic tests
for disappearance before `lstat` and between `lstat` and `unlink`.

### Confirmed fix behavior

- Question replies are published before sending, read back without regeneration,
  and retain exact multipart bytes after first/later-part deferral and process
  reconstruction. The new tests use a changing responder and changed database
  and assert one generation plus identical delivered parts.
- Spool filenames bind bot/update; stored scope hashes bind profile/user/chat.
  Other bindings cannot retrieve the reply. Publication uses a private temporary
  file, fsync, and a non-overwriting hard link. Reads enforce regular-file/0600
  mode, no final symlink, UTF-8, and a 128-KiB reply bound; the directory is 0700.
  Atomic publication cannot overwrite a competing prepared answer.
- Terminal cleanup runs only after `complete_update` succeeds, including retry
  exhaustion. A crash before that commit leaves the reply available for replay;
  a crash after commit leaves an orphan whose durable terminal audit prevents
  regeneration. Cleanup failures leave the orphan for the documented seven-day
  sweep. Expiry can discard pending replay bytes too, and the guide accurately
  discloses that replay is no longer guaranteed after expiry/manual deletion.
  Existing outbound hashes, sent-part skipping, unknown-send handling, and claim
  generation checks remain unchanged. F2 is the remaining sweep defect.
- The PDF imported-to-duplicate case has stable status/text and its new test
  exercises real SQLite/update/messenger delivery. F1 identifies the uncovered
  processing outcomes; it does not invalidate that passing case.
- The urgent guard now prioritizes direct English/Russian emergency statements
  before the narrowed informational exemption. Tests include the reported
  prefix/implicit-pronoun/third-person cases and confirm no retrieval or remote
  responder call; generic informational and blood-pressure negatives remain.
- Generic ferritin/cholesterol trends no longer trigger weight-history refusal.
  Current weight is distinct from weight change. Weight-only change stays local;
  mixed sleep/weight questions carry a typed weight-inference prohibition and
  may answer the supported sleep portion. The revised design explicitly records
  that partial-answer policy and the prompt and local footer retain the limit.
- Current-body queries enforce both inclusive sync-time bounds. Prompt and
  footer disclose exact UTC selection, calendar-date lab resolution, source
  caps, and observation-versus-sync semantics. Stale/future and boundary tests
  cover the original R4 defect.
- The responder now uses explicit low reasoning effort, a 2,000-token default
  (configurable up to 8,000), a 30-second SDK timeout, and no automatic retries.
  Completed-status validation, `store=False`, no chaining, safe exceptions, and
  bounded structured input remain. The 64-character safety hash and tracing
  header do not introduce a provider-idempotency claim.
- Privacy documentation discloses temporary medical answer/footer storage and
  retention. The runtime-composition test explicitly places `telegram_root`
  under `tmp_path`; it does not sweep the runtime's real directory. No new data
  or credential logging was found. The recorded 427-test/static/Alembic gates
  remain implementer evidence, not an independent rerun; F1/F2 lack coverage in
  that reviewed test set.

### Remaining live-only concerns

Real bot/account/model authorization and private delivery, the owner's external
processing decision, synthetic adversarial/clinical evaluation, and useful live
completion rates are still unvalidated. Larger reasoning/output budgets and
bounded SDK calls address the prior configuration concerns but do not prove
latency or clinical accuracy. The reply spool's retention and at-most-once
unknown-send limits should be included in that operational validation. F1/F2
are local code defects and should be fixed before this separate live step.

## Final approval re-review — `283618f`

Reviewed commit `283618f94446f8fdda6fa35f338fdf2e3cf9f1a5` and the resulting
implementation/tests on September 5, 2026. This is the final verdict and
supersedes the preceding failed verdicts for the completed branch.

SPEC PASS

QUALITY APPROVED

OVERALL READY

F1 is resolved. Every successful PDF importer return now produces the same
`received` receipt and “Medical PDF received and stored. It may need review
before use.” acknowledgement. The adapter still waits for the importer/session
to finish successfully and retains the same document ID, hash, and size. It
does not change the document's actual processing status. Parameterized offline
integration cases cover `imported`, `ocr_required`, and `needs_attention`, each
followed by a duplicate after first-send deferral, and assert identical text,
matching attachment audit, sent outbound audit, and terminal completion.

F2 is resolved. The sweep now catches only `FileNotFoundError` around both
entry inspection and expired-entry deletion. Concurrent disappearance is
harmless; other filesystem errors and the existing regular-file/symlink
restrictions remain in force. The new deterministic regression simulates an
entry disappearing during inspection. The deletion race is also covered by
the narrowly scoped handler visible in the code.

All original R1–R4 findings and follow-up F1/F2 findings are closed. No new
high- or medium-priority issue was identified in this final change. Exact
question replay, requester scope, commit-before-cleanup ordering, private
bounded spool storage, documented expiry behavior, SQL isolation, temporal
provenance, urgent handling, safe API configuration, and transport fencing
remain as confirmed above.

The implementer reports 21 focused tests plus Ruff, mypy, and whitespace checks
passing for this final correction. The prior full-suite evidence remains 427
passing tests at the preceding fix revision; this reviewer did not rerun tests
and does not represent that earlier full-suite count as a run of `283618f`.
Only this review report was edited.

“Ready” is approval of the reviewed implementation for its documented scope,
not a claim of live or clinical validation. Real Telegram/OpenAI authorization
and delivery, the owner's data-processing decision, synthetic adversarial and
clinical-quality evaluation, and reasoning-budget/latency calibration remain
live-only work. The disclosed seven-day spool expiry and at-most-once behavior
after unknown delivery remain operational limits.
