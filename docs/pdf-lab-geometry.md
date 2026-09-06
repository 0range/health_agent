# PDF lab geometry extraction

`extract_lab_geometry` is a pure, bounded evidence extractor for two complete,
explicitly headered PDF table layouts. It accepts an exact five-column grid or
an exact four-column word-geometry header backed by drawn table boundaries. The
KDL path maps body words only when five vertical boundaries and horizontal row
boundaries uniquely contain the complete word bboxes; header text alone is not
column or row proof. It returns immutable source cells with page coordinates and
the SHA-256 of the original PDF bytes.

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

The bounded repair API reads only regular, non-symlinked content-addressed PDFs
inside the selected vault, verifies their SHA-256, and scans at most 150 documents,
100 pages per document, 25 MiB per PDF, and 40 observations per page. Dry runs use
rollback-only savepoints and leave no evidence or observation rows behind.
Vault reads walk from the filesystem root through directory descriptors using
`O_DIRECTORY` and `O_NOFOLLOW`, then open the digest-named file relative to the
verified prefix descriptor. This prevents an ancestor-symlink swap between a path
check and the final open.
