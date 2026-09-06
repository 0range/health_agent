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
bounds. Malformed PDFs and invalid requested pages use the single public error
code `invalid_pdf_geometry`; unsupported valid pages return an empty result.
