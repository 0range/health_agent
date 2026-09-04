# Health Question Loop v0.1 Design

**Date:** 2026-09-04
**Status:** Approved implementation scope

## Goal

Answer any free-form Telegram health question using only the bound profile's verified laboratory history and normalized WHOOP history, with clear provenance, uncertainty, and urgent-care escalation.

## Architecture

The existing hardened Telegram update service remains the transport boundary. Its `HealthQuestionService` seam is implemented by a new application service:

`Telegram update -> profile-scoped context builder -> responder -> deterministic sources footer -> Telegram messenger`

The context builder performs read-only, parameterized SQLAlchemy queries with `profile_id` in every predicate. It uses a bounded UTC window (default 30 days; sleep/recovery 14 days; weight/trend 90 days) and fixed per-source limits. It reads only verified lab observations and normalized WHOOP tables; raw payloads, document text, filenames, external account identifiers, and data from other profiles are excluded.

Every evidence item receives a stable citation label such as `[LAB1]`, `[SLEEP1]`, or `[RECOVERY1]`, an observation date, a human-readable metric/value/unit, and a source category. The application always appends the citation mapping itself, so provenance does not depend on model compliance.

## Responder and privacy

A small responder protocol keeps retrieval independent from OpenAI. The production adapter uses the official Responses API (`responses.create`) and reads `response.output_text`. It sends a dedicated medical-safety instruction plus the bounded question/context payload, sets `store=False`, caps output tokens, and uses a one-way profile hash as `safety_identifier`. It does not use conversations or server-side response chaining.

The API key comes from `OPENAI_API_KEY` or a private regular file (`OPENAI_API_KEY_FILE`, default `.tokens/openai-api-key`, mode `0600`, no symlinks). Environment configuration takes precedence. Secret values, questions, evidence, and answers are never logged or printed by status commands. Errors cross application boundaries only as stable safe codes.

The model must distinguish observations from hypotheses, never diagnose or claim causality, say when data is insufficient, and cite supplied labels. A local urgent-phrase guard returns immediate emergency guidance for obvious red-flag wording before any external call.

## Runtime and CLI

- `health-agent question ask --profile-id UUID "..."` runs the same profile-scoped application service and prints the answer.
- `health-agent question status --profile-id UUID` reports only readiness, source counts, and safe configuration/error codes.
- `health-agent telegram run` composes the verified bot credential, SQLite Telegram state, PostgreSQL retrieval, OpenAI responder, existing messenger/update service/long poller, safe command service, and existing medical inbox adapter. It performs no OAuth and never prints tokens.

`/status` is profile-scoped and read-only. `/sync` reports that synchronization must be run with the existing connector commands; this loop does not silently mutate connector state. Attachments keep using the existing medical inbox implementation.

## Failure behavior

Missing data produces an explicit evidence-insufficient answer. Missing/invalid OpenAI configuration, API failures, malformed responses, and database failures become safe user-facing unavailability messages without exception detail. Telegram's existing fenced claims and outbound delivery semantics continue to govern retries and at-most-once unknown delivery.

## Tests and non-goals

Unit and component tests cover window selection, bounded/profile-isolated retrieval, citation construction, safety prompt and urgent guard, secret loading, Responses API arguments, safe errors, command status, and Telegram free-text routing. An integration test composes the loop with fake OpenAI/Telegram transports and a test database. No test uses real tokens, OAuth, personal data, or network access.

Calendar, reminders, diagnoses, treatment decisions, conversational memory, vector search, and new connector synchronization are out of scope.
