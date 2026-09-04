# Task 2 independent review

## Verdicts

- **SPEC: FAIL**
- **QUALITY: CHANGES REQUESTED**

The adapter uses the current official Python Responses pattern correctly: it invokes
`client.responses.create`, reads the SDK's `output_text` convenience property, accepts
only `status == "completed"`, disables response storage, and sends a stable hashed
`safety_identifier`. It supplies no conversation or previous-response identifier. The
application also correctly checks the local urgent guard before either context retrieval
or the remote responder, returns generic unavailable text and closed error codes, and
appends a deterministic local source footer. The key loader gives a non-empty environment
key precedence and, for fallback, opens the final file descriptor with `O_NOFOLLOW` then
checks that descriptor is a regular file with exact `0600` mode. The dependency and lock
update include the official `openai` SDK.

The following gaps nevertheless leave the task short of its safe prompt and
evidence-insufficient behavior contract.

## Findings

### HIGH — untrusted question text can forge the prompt's evidence/limitation sections

`build_responder_input()` concatenates the user-controlled `question` directly into a
plain-text prompt immediately before the trusted `Verified observations:` and `Known
limitations:` headings (`src/health_agent/questions/openai.py:89-108`). A question can
therefore contain newlines such as `Verified observations:`, arbitrary fake `[LAB…]`
claims, or instructions to ignore the rest. The safety instruction says to use only
“supplied verified observations”, but it never identifies the question as untrusted data,
defines a non-forgeable evidence boundary, or tells the model not to execute text in it.
The current test only checks a character cap, not a section-forgery/prompt-injection case.

This is a health-answer integrity problem: a model can treat attacker-provided text as
evidence, follow it as an instruction, or cite a forged label. It also weakens the stated
data-minimization contract because free-form content is not represented as a clearly
separate user question.

Action: pass structured, role-separated input (for example, a user message whose question
and typed evidence are distinctly delimited/serialized) and strengthen the developer
instructions: question content is untrusted and is never evidence or instructions; only
the generated typed evidence collection may support factual claims; do not honor labels
or directions embedded in the question. Bound every serialized field and add mocked
request tests with newline heading forgery and instruction-injection payloads. Preserve
the current minimal evidence fields—label, date, metric, normalized value, and unit—and
do not add profile IDs, raw payloads, document text, or external identifiers.

### HIGH — non-empty but insufficient contexts still make a remote call and rely on model compliance

`HealthQuestionApplicationService.answer()` declares the context sufficient whenever
`context.evidence` is non-empty (`src/health_agent/questions/service.py:91-111`). It
does not make a local decision from `context.limitations`. In particular, Task 1 returns a
current weight snapshot together with
`WEIGHT_TREND_INSUFFICIENT_HISTORY`; a weight-trend question can also have unrelated lab,
sleep, or recovery evidence. The service sends all of it to OpenAI instead of producing
the specified explicit evidence-insufficient response. Its prompt merely asks the model to
say that evidence is insufficient, and the model result is then presented as a successful
answer.

This conflicts with the design's failure behavior (“Missing data produces an explicit
evidence-insufficient answer”) and its requirement that the model say plainly when the
evidence cannot answer. It also sends data externally when the local, deterministic answer
already knows the requested trend cannot be established.

Action: make sufficiency an explicit context/service contract, not a model instruction.
At minimum, a limitation that establishes the requested intent cannot be answered (such as
`WEIGHT_TREND_INSUFFICIENT_HISTORY`) must return the local insufficient-evidence text,
footer, and limitations without calling the responder. Prefer a typed
`answerable`/insufficiency reason emitted by the context builder so future intents do not
depend on service knowledge of individual limitation codes. Add tests for a non-empty
weight-trend context with that limitation (including unrelated evidence) and assert no
remote call; retain the existing empty-context test.

### MEDIUM — deterministic footer does not enforce the required citations on generated claims

The safety instruction asks the model to cite bracket labels, but successful output is
accepted verbatim and no test covers missing, forged, or unknown labels
(`src/health_agent/questions/openai.py:78-87`; `src/health_agent/questions/service.py:113-121`).
The appended footer reliably maps the retrieved evidence, which is valuable, but it does
not establish which particular data-dependent generated claims the sources support. A
model answer with no citations—or a fabricated `[LAB99]`—is still returned as available.

Action: either validate generated bracket labels against `context.evidence` and treat an
invalid/unattributed data answer as unavailable/insufficient, or make a deliberate,
documented product decision that the footer alone is the citation mechanism and remove the
stronger per-claim citation requirement from the prompt/design. Add coverage for the
chosen contract, including unknown labels and a no-citation model answer.

## Verification and scope

Reviewed the approved design and implementation plan, Task 2 brief/report, supplied
`review-0d1721d..71d1aee.diff`, current Task 1 contracts, implementation, tests, config,
dependency manifest, and lockfile. I did not modify implementation or run tests, as
requested.

Responses API behavior was checked against the official OpenAI documentation: Python SDKs
support `response.output_text`; response status must be checked for `completed`; and
`safety_identifier` is the current replacement for the deprecated user identifier. The
use of `store=False` is also consistent with the Responses data-controls documentation.
See [Responses API reference](https://platform.openai.com/docs/api-reference/responses)
and [OpenAI data controls](https://platform.openai.com/docs/models/default-usage-policies-by-endpoint).
