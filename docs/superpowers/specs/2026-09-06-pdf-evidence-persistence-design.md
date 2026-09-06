# Immutable PDF table evidence

TL;DR: save proven PDF cells alongside, never instead of, existing page text; link each new pending observation to that evidence. Initial imports and explicit archive repair use the same path.

User already approved broad ingestion and autonomous repair. Replacing extracted text would invalidate old observations; putting derived text in a fake new page would invent page provenance. Choose a small immutable evidence table keyed by real document/page/method/source hash, plus nullable observation FK. Existing flat/OCR observations retain null evidence ID and unchanged source text. Source bboxes and exact cell text remain inspectable JSON, derived row string explicitly identified as reconstructed table evidence.

The reviewed geometry adapter is deterministic and local, no added cloud calls. Hash and profile checks happen before accepting evidence. Unsupported pages are counted honestly, not silently marked fully parsed. New rows start NEEDS_REVIEW. No bulk approval or rejection within repair; operator root later validates actual data separately. Review corrections retain evidence lineage. Repair is idempotent across all review statuses and bounded; never reset queue versions, cloud budgets or legacy per-page candidate limits.

No date guess: use existing explicit medical-date API later with source proof. No scan of other profiles. No original files, bboxes, medical text or raw exceptions committed/logged.
