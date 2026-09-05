# Telegram capture v0.1 — implementation handoff

Branch: `codex/v1-telegram-capture`, based on `b5147c7`.
Design: `docs/superpowers/specs/2026-09-05-telegram-capture-design.md`.
Plan: `docs/superpowers/plans/2026-09-05-telegram-capture.md`.

## Delivered scope

- Production `telegram run` accepts JPEG/PNG photographs and image documents through the shared medical importer, alongside the existing PDF path. Original bytes remain in the private content-addressed vault; document/profile deduplication and separate source provenance links are retained.
- Size is capped at 20 MiB and dimensions at 25 million pixels before decoded allocation. Signature/MIME, PNG structure/checksum/animation, JPEG header, and full decoding are checked. Private staging and inbox temporary copies are cleaned after success and handled failures.
- On macOS, a fixed local Apple Vision Swift program attempts English/Russian OCR without cloud requests. Execution is limited to 30 seconds and 100000 characters; unavailable/failed recognition stores the original with truthful `ocr_required`, not fabricated data. Duplicates do not rerun OCR or alter inferred dates from a different OCR result.
- `/review` displays one unverified lab candidate with document UUID, page, extracted value/unit and collection/issue date. Only `/confirm ITEM_UUID`, `/correct ITEM_UUID VALUE UNIT`, and `/reject ITEM_UUID` apply an explicit decision. Free-form questions never mutate data. Candidate lookup and decisions require this profile plus a Telegram source occurrence from this bot/private chat.
- Corrections use existing immutable lineage: the original source row is retained and rejected, one replacement version is verified. Row locks serialize review/document status updates. Same-decision replay gives the same acknowledgment; conflicting resolved decisions are refused. Question retrieval sees only verified normalized results.
- Production review and question replies share the existing private replay spool and terminal cleanup. Existing outbound multipart/429/unknown-send, requester binding, bot namespace and claim completion fencing are unchanged. A DB commit before reply-spool creation is recoverable because decisions replay from durable review state.
- CLI `review correct ITEM_UUID --value VALUE --unit UNIT --profile-id PROFILE_UUID` exposes the existing correction core without printing exception details. `import-file` also accepts JPEG/PNG. Telegram help and operation/privacy docs reflect the actual implementation.

## Intentional exclusions

WHOOP is the sole v0.1 weight source: **no manual Telegram weight**. Manual symptom, medicine and supplement capture is deferred. No image generation, cloud OCR, voice transcription, general medical narrative extraction, bulk historical review or arbitrary correction of already-verified facts was added. Lab parser vocabulary and normalization remain the existing supported set. Missing/wrong medical dates need explicit local `review set-date`.

An empty review queue does not establish that every upload was read. If local OCR is unavailable, originals remain `ocr_required`; this slice does not add an OCR-reprocess command. Owner intervention/future reprocessing is required. Crash-killed processes may leave private staging/temp files; they are not covered by the prepared-reply TTL.

No DB migration was needed: Document, DocumentPage, SourceRecord/DocumentSourceRecord, ReviewItem and correction lineage already represent this slice. The existing single-head migration chain is preserved.

## Verification and review status

TDD evidence: text-action module and image module initially absent; image inbox initially returned needs_attention; review module/CLI correction initially absent; duplicate-image re-OCR regression initially failed. Focused passes followed each implementation. Final tests use only synthetic data, fake Telegram/Responses/OCR boundaries and disposable local PostgreSQL.

| Gate | Result |
| --- | --- |
| `uv run --offline pytest -q` | 630 passed; five existing SWIG deprecation warnings |
| `uv run --offline ruff check .` | PASS |
| `uv run --offline mypy src` | PASS, 72 files |
| mypy src plus all changed test files | PASS, 79 files |
| `uv run --offline mypy src tests` | 13 inherited errors in unchanged automation/Drive/staging tests; not claimed clean |
| `uv run --offline alembic heads` | one head, `0005_whoop` |
| Disposable fresh schema roundtrip | PASS in full suite (`test_fresh_migrations_can_downgrade_to_base_and_upgrade_again`) |
| `git diff --check` | PASS |

The production integration regression covers JPEG photo -> vault/pending candidate -> single-item review -> confirm -> first-send 429 -> fresh runtime/exact acknowledgment -> duplicate suppression -> question using verified evidence, with staging/temp/spool cleanup. Separate tests cover profile/chat/bot/source isolation, correction replay/lineage/concurrency, invalid input, OCR time/output/error bounds and canonical image/PDF receipts.

Brainstorming and writing-plans produced the scoped design/plan before implementation. SDD workspace tracks tasks and RED/GREEN evidence. All four team slots were occupied; parent explicitly authorized local implementation/full gates with independent review later. **Self-review complete; independent review pending. This report does not claim independent SPEC/QUALITY approval.**

## Live-only validation

No real token, personal data, live bot, cloud service or actual OCR invocation was used. Owner must separately verify installed Swift/Apple Vision availability, recognition accuracy and orientation/language behavior with non-sensitive photos, plus actual Telegram photo/document delivery and the existing OpenAI account configuration. Local OCR timeout may include Swift startup/compilation. OCR-derived values and dates must be checked against the original before relying on them.

No merge or push to main performed. To combine reminders, compose `TelegramReviewActions(engine)` and `DatabaseReminderCommands(engine)` in one `CompositeTelegramTextActions`, wrapped once by `PreparedTelegramTextActions` using the same question reply store.
