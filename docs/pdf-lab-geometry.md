# PDF lab geometry extraction

`extract_lab_geometry` is a pure, bounded evidence extractor for complete,
explicitly headered PDF table layouts. It accepts an exact five-column grid or
an exact four-column word-geometry header backed by drawn table boundaries. The
KDL path maps body words only when five vertical boundaries and horizontal row
boundaries uniquely contain the complete word bboxes; header text alone is not
column or row proof. It returns immutable source cells with page coordinates and
the SHA-256 of the original PDF bytes.

The additional exact five-column headers are `Параметр / Значение / Ед. измер. /
Реф.значение / Представление` and `Показатель / Результат / Ед. изм. /
Референсные пределы / Комментарий`. Both map name, result, unit, reference,
and comment in that order. Whitespace (including wrapped headings) is folded.
A header may follow fully merged preamble rows; each physical table must have
exactly one supported header and body cells must match its ordered column bounds.
Separate physical tables can each contribute rows. Merged footer text is ignored.
Representation strings such as `[-*-]` and `[---]*` remain comments, never flags.
An attached flag in the numeric result is still rejected.

Total prolactin accepts the exact additional alias `Пролактин / Prolactin`.
`monomeric_prolactin` is distinct and recognizes `Пролактин мономерный (пост ПЭГ)`,
`Пролактин мономерный (пост-ПЭГ)`, and `Monomeric prolactin`. Both accept literal
`мЕд/л` as the separate `mU/L` family, without conversion to international or mass
units. Monomeric prolactin also accepts explicit `mIU/L`, `uIU/mL`, and `ng/mL`.
Source names and units remain unchanged. Signature dates and blank collection
labels do not establish a collection date.

Rows are emitted only when the analyte is registered, the source unit is
compatible, the reference cell is present, and the result is one unflagged
numeric token. Unknown names, incompatible units, merged required cells,
partial or duplicate headers, ambiguous numeric cells, free-standing numeric
bands, cross-column/row words, unbounded wrapped names, and flagged values remain
unresolved. The extractor does not parse or
infer dates, rewrite source fields, persist observations, or alter the input.

The implementation has explicit byte, page, table, word, row, cell, and output
bounds. Vector geometry is additionally capped at 512 drawing paths and 4,096
nested drawing items. PyMuPDF's drawing API materializes its native result as one
call; the returned collection is size-checked before Python geometry accumulation
or interpretation. Malformed PDFs and invalid requested pages use the single
public error code `invalid_pdf_geometry`; unsupported valid pages return an empty
result.

## Immutable persistence

Supported geometry can be stored as one immutable `page_evidence` record per
document, page, method, and exact source hash. JSON retains every source cell and
bbox. Pending observations reference that same document/page evidence through a
composite foreign key. Replays compare exact JSON and never overwrite evidence;
complete source identity deduplicates observations across every review status.

New headers, headers following merged preambles, and rows enabled by the added
registry aliases or unit family use `pdf_table_v2`. Unchanged supported pages
retain `pdf_table_v1`; new rows never rewrite existing v1 evidence. This versioning
also applies when a newly registered analyte occurs under an original header.
Migration `0017_pdf_evidence_v2` extends only the evidence-method check constraint.
Its downgrade locks the evidence table and refuses to proceed if any v2 evidence
exists; it never deletes or relabels those records.

The bounded repair API reads only regular, non-symlinked content-addressed PDFs
inside the selected vault, verifies their SHA-256, and scans at most 150 documents,
100 pages per document, 25 MiB per PDF, and 40 observations per page. Dry runs use
rollback-only savepoints and leave no evidence or observation rows behind.
Vault reads walk from the filesystem root through directory descriptors using
`O_DIRECTORY` and `O_NOFOLLOW`, then open the digest-named file relative to the
verified prefix descriptor. This prevents an ancestor-symlink swap between a path
check and the final open.
