# PDF laboratory evidence

TL;DR: preserve existing flat page text and add a separately identified, reproducible table extraction. Never manufacture a contiguous original-text excerpt by joining unrelated columns.

The authorized archive audit found readable laboratory tables whose default PDF text is column-major. Strict text extraction correctly emits no results from them. Selected solution: map exact unambiguous table headers to physical cells, retain their original text/coordinates and a source-byte hash, then parse a deterministic derived row. Alternatives rejected: overwrite existing page evidence (breaks provenance), infer rows from loose numerical proximity (wrong-patient/column risk), send the entire archive repeatedly to a model (does not prove the transcription).

First extraction supports the observed five-column gridded layout and KDL's four-column headered layout. Unknown headers, duplicate result columns, intersecting cells, multiple numeric results, incompatible names/units remain unresolved. Original PDF is always accessible through document/page identity. Clinical interpretation and transcription approval remain separate.

Implementation is two independent gates: pure geometry extraction tested on synthetic PDFs; then immutable alternate representation persistence, importer integration and explicit existing-archive repair. No automatic confirmation of clinical results and no hidden review-queue reset. Existing erroneous draft observations can later be rejected with an explicit operator audit while preserving originals and rejected rows.
