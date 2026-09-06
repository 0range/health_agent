# Clinical report context

Free health questions can use up to five source-anchored clinical document excerpts and
five saved visit answers from the selected profile. This material is serialized as
`reported_material`, separately from verified observations. Document text must begin at
an exact supported section heading; no whole-page fallback, date inference, or numeric
table promotion occurs. Cancelled visits, questions, future records, and unreadable or
integrity-failed originals are excluded.

Each item carries a deterministic public citation, an internal record reference, and
explicit date semantics. A document medical date is used only when stored and not in the
future. Otherwise the timestamp is labelled as local archive time. Visit-answer time is
the note creation time, not evidence of clinician authorship. Provider text is treated as
quoted data, including any embedded directions or citation-like strings.
