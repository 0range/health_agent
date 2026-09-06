# Medical date recovery

TL;DR: recover only explicit collection/issue dates from stored document text. Never use Drive, email, import timestamps, birth dates, study dates or result readiness as substitutes. Existing dates and reviewed lab values stay untouched.

This is the already-requested local medical-import repair. The corrected archive audit found 15 documents with collection labels; the other 17 date-bearing candidates were study/readiness dates, NOT issue dates.

Use one pure parser for new imports and a bounded profile-scoped backfill. It preserves per-page evidence positions and accepts only a known label immediately followed by a numeric date in the same field, or a label-only line followed by one date-only line (optional time). Conflicting/invalid/future candidates are not guessed. Dates are calendar-validated. A dry run is the default; explicit apply only fills empty columns after locking/re-reading stored pages and checking chronology. No review/status/conflict clearing, no external calls and no lab approvals. Evidence stays reconstructible from immutable stored page text and parser version; no new database subsystem.

Acceptance covers numeric formats, real label suffixes, field/page boundaries, date-role separation, duplicates/conflicts, existing date preservation, isolation, dry-run no mutation and repeat-apply no-op. Synthetic tests precede an aggregate-only dry run on the archive; only unambiguous results may be applied.
