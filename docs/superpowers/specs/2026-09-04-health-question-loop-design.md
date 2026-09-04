# Health Question Loop v0.1 Design

**Date:** 2026-09-04
**Status:** Approved implementation scope

## Goal

Answer any free-form Telegram health question using only the bound profile's verified laboratory history and normalized WHOOP history, with clear provenance, uncertainty, and urgent-care escalation.

## Architecture

The existing hardened Telegram update service remains the transport boundary. Its `HealthQuestionService` seam is implemented by a new application service:

`Telegram update -> profile-scoped context builder -> responder -> deterministic sources footer -> Telegram messenger`

The context builder performs read-only, parameterized SQLAlchemy queries with `profile_id` in every predicate. It uses a bounded UTC window (general/current weight 30 days; sleep/recovery 14 days; explicit weight change 90 days) and fixed per-source limits. It reads only verified lab observations and normalized WHOOP tables; raw payloads, document text, filenames, external account identifiers, and data from other profiles are excluded. Both inclusive bounds also apply to body sync-as-of timestamps; lab dates have calendar-day resolution. The exact selected interval, cap, and time semantics are included in the model input and local footer. Weight-change limitations apply independently of window choice, allowing supported sleep portions of mixed questions while forbidding unsupported weight trends.

Every evidence item receives a stable citation label such as `[LAB1]`, `[SLEEP1]`, or `[RECOVERY1]`, an observation date, a human-readable metric/value/unit, and a source category. The application always appends the citation mapping itself, so provenance does not depend on model compliance.

## Responder and privacy

A small responder protocol keeps retrieval independent from OpenAI. The production adapter uses the official Responses API (`responses.create`) and reads `response.output_text`. It sends a dedicated medical-safety instruction plus the bounded question/context payload, sets `store=False`, caps output tokens, and uses a one-way profile hash as `safety_identifier`. It does not use conversations or server-side response chaining.

The API key comes from `OPENAI_API_KEY` or a private regular file (`OPENAI_API_KEY_FILE`, default `.tokens/openai-api-key`, mode `0600`, no symlinks). Environment configuration takes precedence. Secret values, questions, evidence, and answers are never logged or printed by status commands. Errors cross application boundaries only as stable safe codes.

The September 5 review fix adds temporary reply-only delivery storage, separate
from content-free Telegram state. The final reply and opaque scope hash are
atomically published in a `0700` directory as a bounded `0600` regular file before
sending. A bot/update hash identifies the reply; profile/user/chat scope must
match on replay. No question, raw retrieval context, or conversation history is
stored. Retries and restarts reuse exact final reply bytes, including the footer.
Only a committed terminal update permits deletion; startup/incoming requests
sweep orphan files older than seven days. Existing outbound content conflicts
and unknown-send fencing remain authoritative. The provider request-ID header
is for tracing only; no Responses API idempotent-replay guarantee is assumed.

The model must distinguish observations from hypotheses, never diagnose or claim causality, say when data is insufficient, and cite supplied labels. A local urgent-phrase guard returns immediate emergency guidance for obvious red-flag wording before any external call.

## Runtime and CLI

- `health-agent question ask --profile-id UUID "..."` runs the same profile-scoped application service and prints the answer.
- `health-agent question status --profile-id UUID` reports only readiness, source counts, and safe configuration/error codes.
- `health-agent telegram run` composes the verified bot credential, SQLite Telegram state, PostgreSQL retrieval, OpenAI responder, existing messenger/update service/long poller, safe command service, and existing medical inbox adapter. It performs no OAuth and never prints tokens.

`/status` is profile-scoped and read-only. `/sync` reports that synchronization must be run with the existing connector commands; this loop does not silently mutate connector state. Attachments keep using the existing medical inbox implementation.

## Failure behavior

Missing data produces an explicit evidence-insufficient answer. Missing/invalid OpenAI configuration, API failures, malformed responses, and database failures become safe user-facing unavailability messages without exception detail. Telegram's existing fenced claims and outbound delivery semantics continue to govern retries and at-most-once unknown delivery.

## Tests and non-goals

Unit and component tests cover window selection, bounded/profile-isolated retrieval, citation construction, safety prompt and urgent guard, secret loading, Responses API arguments, safe errors, command status, and Telegram free-text routing. Integration tests compose the loop with fake OpenAI/Telegram transports and a disposable database, including first-part/later-part 429, restart replay, and imported-PDF duplicate acknowledgements. No test uses real tokens, OAuth, personal data, or live service requests. Database fixtures use local Docker/TCP and the already-cached PostgreSQL image.

Calendar, reminders, diagnoses, treatment decisions, conversational memory, vector search, and new connector synchronization are out of scope.
