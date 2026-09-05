# Telegram Capture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest Telegram medical images and safely resolve one pending laboratory observation with explicit commands.

**Architecture:** Extend the shared original-document importer with bounded local image OCR; compose explicit profile-scoped review actions behind the existing Telegram authentication and delivery spool. Preserve existing review lineage and SQL schema.

**Tech Stack:** Python 3.13, SQLAlchemy/PostgreSQL, PyMuPDF, local Apple Vision via Swift, Typer, pytest.

## Global Constraints

- Weight comes from WHOOP in v0.1; do not implement manual Telegram weight.
- No live network services, real tokens, OAuth, or personal health data.
- Preserve profile isolation, source provenance, PDF/question behavior, claim fencing, exact reply retries, and at-most-once unknown delivery.
- No free-text silent mutation; only explicit commands can approve/correct/reject pending observations.
- JPEG/PNG inputs are bounded to 20 MiB, 25 million pixels, and one image; local OCR timeout is 30 seconds and output cap is 100,000 characters.
- No raw health text, dialogue, original files, or secrets in operational logs or error messages.
- Use apply_patch for authored files; work on codex/v1-telegram-capture from b5147c7, never merge/push main.
- Preserve existing DB schema and migration lineage; confirm with disposable Alembic checking.

---

### Task 1: Replay-safe Telegram action seam

**Files:** Modify `src/health_agent/telegram/{types,service}.py`; create `src/health_agent/telegram/actions.py`; modify `src/health_agent/questions/composition.py`; create `tests/telegram/test_actions.py`.

**Interfaces:** Consumes existing `MessageContext` and `PrivateReplyStore`. Produces `TelegramTextActionService.handle(context: MessageContext, text: str) -> str | None`, `CompositeTelegramTextActions(handlers)`, `PreparedTelegramTextActions(handler, reply_store)`, and optional `TelegramUpdateService(..., text_actions=None)`.

- [ ] Add tests for dispatch ordering, unknown text returning None, existing freeform/status behavior, and repeated prepared replies:

```python
first = actions.handle(context, "/review")
handler.reply = "changed"
assert actions.handle(context, "/review") == first
assert handler.calls == 1
```

- [ ] Run `uv run --offline pytest -q tests/telegram/test_actions.py` and record the expected missing-interface failure.
- [ ] Implement the protocol and first-non-None composite. The service calls optional actions after existing help/status/sync and before unknown-command fallback. The prepared wrapper reads the existing scoped spool first, calls the handler once when absent, and publishes only non-None replies.

```python
prepared = reply_store.get(context)
if prepared is not None:
    return prepared
reply = handler.handle(context, text)
return None if reply is None else reply_store.put(context, reply)
```

- [ ] Run focused action/Telegram/question tests and static checks. Commit `feat: add replay-safe Telegram text actions` and provide the seam commit to the reminders slice.

### Task 2: Bounded original-image ingestion

**Files:** Create `src/health_agent/images.py`; modify `src/health_agent/pdf.py`, `src/health_agent/importer.py`, `src/health_agent/questions/composition.py`; create `tests/test_images.py`; extend `tests/test_importer.py` and `tests/questions/test_composition.py`.

**Interfaces:** Produces `extract_image(path: Path, expected_media_type: str | None = None) -> tuple[str, ExtractedPdf]` with detected MIME and existing extraction result contract. `import_document(..., media_type: str | None = None)` accepts an optional signature-validated MIME and otherwise detects its supported original type. Existing PDF callers require no change.

- [ ] Create synthetic PNG/JPEG fixtures with PyMuPDF. Write tests that expect image MIME/original SHA, one `local_ocr` page and pending candidate with fake OCR, same-image source dedupe, cross-profile independence, invalid/oversized image rejection before vault writes, and `ocr_required` when local recognition is unavailable.

```python
report = import_document(session, vault, image, None, profile_id=profile_id)
document = session.get_one(Document, report.document_id)
assert document.media_type == "image/png"
assert Path(document.vault_path).read_bytes() == image.read_bytes()
assert observation.status is ReviewStatus.NEEDS_REVIEW
```

- [ ] Run `uv run --offline pytest -q tests/test_images.py tests/test_importer.py` and record expected image-contract failures before implementation.
- [ ] Detect PDF/JPEG/PNG from bytes; validate image byte count, metadata dimensions, allowed MIME, and complete decoding before persistence. Invoke fixed local Vision script through `subprocess.run` with `capture_output=True`, `timeout=30`, no shell, and controlled output. Return `ocr_required` on unavailable local OCR; never substitute ingestion time for medical time.
- [ ] Extend the common importer and Telegram inbox for images while preserving PDF receipt wording/replay where possible. Non-medical audio remains truthful needs-attention. Remove transient bytes on all exceptions/interrupts.
- [ ] Run focused image/importer/inbox tests and static checks; commit `feat: ingest medical images through the shared vault`.

### Task 3: Explicit single-item review and complete gates

**Files:** Create `src/health_agent/telegram/review.py`, `tests/telegram/test_review.py`; modify `src/health_agent/questions/composition.py`, `src/health_agent/cli.py`; extend `tests/questions/test_question_loop_integration.py`, `tests/test_review_cli.py`; create `docs/telegram-capture.md`, `docs/superpowers/reports/2026-09-05-telegram-capture-report.md`; minimally update README.

**Interfaces:** `TelegramLabReviewService(engine).handle(context, text)` implements the Task 1 protocol. It uses the existing importer approve/correct/reject methods and scopes source joins to the bound profile and Telegram provenance. Production composition wraps the review handler with the shared reply store.

- [ ] Seed two profiles, PDF/image documents and pending candidates. Add tests for one-item `/review`, explicit confirm/correct/reject, unchanged free text, forbidden foreign/source UUIDs, unsupported units, replay matching/conflicting decisions, and verified evidence only after explicit confirmation.

```python
before = builder.build(profile_id, "ferritin")
assert not before.evidence
review.handle(context, f"/confirm {observation_id}")
after = builder.build(profile_id, "ferritin")
assert after.evidence[0].value == "42"
```

- [ ] Run the review tests and record RED failures. Implement bounded command parsing, locked profile/source-scoped queries, explicit existing mutation methods, stable acknowledgements and safe errors. `/review` lists one candidate with date/page/source value and concrete command syntax; unsupported/missing date facts stay explicitly uncertain.
- [ ] Add `review correct OBSERVATION_UUID --value VALUE --unit UNIT --profile-id UUID` to CLI with stable errors. Wire review actions into production without altering question/PDF routing; add static `/review` guidance to canonical upload receipts and help.
- [ ] Add an integration test through real Telegram state/messenger: upload/import fixture, pending item, explicit command, cited question evidence, first-send 429 and restart replay. Preserve existing PDF retry tests.
- [ ] Run `uv run --offline pytest -q`, `uv run --offline ruff check .`, `uv run --offline mypy .`; verify disposable `tests/whoop/test_schema.py::test_whoop_migration_matches_sqlalchemy_metadata` runs in the suite. Record precise results and live-only limits.
- [ ] Perform self-review and independent task/whole-branch review when an agent slot is available; commit implementation and final report. Return exact delivered scope, exclusions, commits, verification, and no merge/push.
