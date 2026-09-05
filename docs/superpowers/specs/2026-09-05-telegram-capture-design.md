# Telegram capture v0.1

Date: September 5, 2026. Implementation scope derives from the approved personal
health-agent v1 design and the subsequent explicit owner correction: weight comes
from WHOOP in v0.1; do not implement manual Telegram weight.

## Scope and choices

Deliver image ingestion and explicit review of one pending laboratory observation
from a newly received Telegram document. Manual symptom/medicine/supplement capture
is a separate optional slice; it is not required for this focused delivery and is
not implemented here. Existing question answering, PDF ingestion, source isolation,
and at-most-once unknown Telegram delivery must remain intact.

Three approaches were considered: store images awaiting a later OCR installation;
send images to a cloud vision service; or use local Mac OCR and the existing
document/review pipeline. Choose local Mac OCR, with truthful `ocr_required` when
the platform service is unavailable. It gives photographs a usable extraction path
without introducing remote image processing or a second document database.

## Image boundary

The shared importer accepts PDF, JPEG, and PNG originals. The detected file
signature, not extension, determines MIME. Image metadata and complete decoding
must agree with the expected MIME. Image input is bounded to 20 MiB, 25 million
pixels, and one frame/page. Unsupported, malformed, or oversized images fail
before vault persistence. PDF behavior remains on the existing extraction path.
JPEG validation walks metadata segments and all entropy-coded scans through the
single terminal EOI; appended payloads, concatenated images, additional frames
and MPF containers are rejected before decoding or OCR.

Images use a local Apple Vision recognizer invoked by `/usr/bin/swift` with a
fixed script and an argument list, never a shell. Recognition is bounded by a
30-second subprocess timeout and 100,000 output characters. No image or text is
sent externally. Missing platform tooling or recognition failure yields an empty
page marked `ocr_required`; extracted text is marked `local_ocr` and every parsed
laboratory value remains `needs_review`. No automated medical confirmation occurs.

The original image bytes are stored in FileVault, and Document/DocumentPage,
SourceRecord, and DocumentSourceRecord retain MIME, page, Telegram provenance,
medical dates when explicitly recognized, and content-hash deduplication within
the profile. The same image arriving through another source deduplicates through
the shared importer. Existing reviewed values are never replaced on duplicate
import. No migration is needed: current string fields and existing review lineage
represent all state.

## Telegram review

An optional `TelegramTextActionService.handle(context, text) -> str | None` seam
runs after existing help/status/sync handling and before unknown-command fallback.
A composite dispatcher supports independently developed review/reminder handlers.
Recognized action replies reuse the existing private reply spool for exact retry
bytes. Ordinary free text continues to the question service.

`/review` returns at most one pending item from the bound profile's Telegram
documents, including its stable observation UUID, date or missing-date warning,
page, recognized metric/value/unit, and explicit commands:

- `/confirm OBSERVATION_UUID`
- `/correct OBSERVATION_UUID VALUE UNIT`
- `/reject OBSERVATION_UUID`

The observation UUID identifies the displayed fact. These commands are explicit
confirmation; no free-text phrase such as “yes” mutates data. Mutations require
the current authenticated profile and Telegram document provenance, take a row
lock, and invoke the established approve/correct/reject functions. Corrections
retain the original source and create the existing superseding observation.
Already-resolved values are immutable. Repeating the same decision returns the
same acknowledgement; conflicting decisions report that the item was already
resolved and make no change. Unknown/foreign UUIDs share one generic response.
The shared numeric parser refuses NaN/infinities and bounded-format violations
before any verified-state mutation: at most 64 input characters, 28 significant
digits, stored decimal exponent -12 through 12 and absolute value at most 10^12.
These are technical bounds, not claims about medically normal ranges; a value
outside them remains pending and requires local investigation. Ordinary signed
decimals, comma decimals and bounded scientific notation remain supported.

Upload receipts use stable, truthful text and direct the user to `/review`, so
queue changes do not alter a deferred upload acknowledgement. Command replies
do not append the next item after mutation; the user requests it with `/review`.
The existing local review CLI remains available, and gets a `review correct`
command using the same correction contract.

## Validation and privacy

Tests use synthetic raster/PDF fixtures, fake local OCR and fake API transports,
disposable PostgreSQL, private temporary roots, and real Telegram state/messenger
where delivery behavior matters. Cover MIME mismatch, invalid image, pixel/byte
bounds, local-OCR unavailable/error, original-byte dedupe, provenance, cleanup,
single-item source/profile isolation, all three review decisions, conflicting
replays, restart/429 reuse, and verified-only question retrieval after confirmation.
Run focused RED/GREEN cycles, the full offline suite, Ruff, mypy, and disposable
Alembic metadata checking. No real network service, token, OAuth, or personal
record may be used. Report live OCR quality/Telegram delivery as unvalidated.
