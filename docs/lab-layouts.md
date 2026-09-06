# Supported local laboratory layouts

Local extraction creates review candidates only for exact registered analyte aliases
paired with compatible registered units. Unknown or incomplete rows remain unresolved;
they are never promoted to verified observations automatically.

Supported source-preserving layouts are:

- one line in `analyte value unit [reference]` order, including printed qualifiers,
  bounded scientific notation, and supported flag positions;
- exactly three or four pipe- or tab-separated fields in either
  `analyte | value | unit | reference` or
  `analyte | unit | value | reference` order;
- one complete labelled record of at most eight lines and 1,000 characters, using
  exact Russian or English analyte, result, unit, and optional reference labels.

Extra columns, competing labels, multiple records in one evidence excerpt, arbitrary
field permutations, unknown names or units, and protocol-like numeric prose are not
accepted locally. The stored excerpt remains an exact substring of the unchanged page
text. Every accepted result still requires explicit human review.
