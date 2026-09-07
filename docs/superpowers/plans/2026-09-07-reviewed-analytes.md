# Reviewed Archive Analytes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. User asks finish existing medical archive; parallel disjoint work authorized.

**Goal:** Permit explicit source-reviewed corrections for remaining analytes without guessing specimen or changing historical parser evidence.

**Architecture:** Add canonical-only registered identities for operator review, using existing correct_observation provenance. Existing raw-name auto-mapping remains unchanged; reconcile known saved dashboard queries from previous registry.

**Tech Stack:** Python, existing registry and Metabase query generation, pytest/PostgreSQL.

## Global Constraints

- No PHI/source values in Git; private source examples are data/pilot/archive-recovery/remaining-existing-canonical-map-20260907.json. No live writes/cloudcalls by implementer.
- Preserve existing raw aliases/units/parser outputs and immutable pdf_table_v1/v2 evidence. New definitions use canonical-only names, NOT source-language aliases; root reviews and explicitly corrects each source row with existing lineage-preserving API.
- Keep saliva/urine/percentage/concentration/individual blood fractions distinct; no automatic clinical interpretation or guessed unit conversions. Preserve unrelated dirty files and other sleep worker changes.
- No schema/migration/service changes. Exact old dashboard SQL must remain recognizable; do not adopt edited/unownedcards.

## Task 1: Canonical-only reviewed analytes and chart compatibility

**Files:** lab_extraction/registry.py, lab_dashboard.py, tests/lab_extraction/test_registry.py (or existing registry coverage), tests/test_lab_dashboard.py; one short docs section. No PDF/parser/service/panel/brain changes.

**Interfaces:** Existing normalize_registered and correct_observation APIs unchanged. Append definitions with empty raw alias string (canonical name still maps by existing _NAMES construction). Add readable labels separately in lab_dashboard._LABELS, not as parseraliases. Exact IDs/families:

- indirect_bilirubin:umol/L|mg/dL; thrombocrit:%; myelocytes:%; metamyelocytes:%; band_neutrophils:%; segmented_neutrophils:%.
- reticulocytes:%; immature_reticulocyte_fraction:%; low_fluorescence_reticulocyte_fraction:%; medium_fluorescence_reticulocyte_fraction:%; high_fluorescence_reticulocyte_fraction:%.
- monomeric_prolactin_recovery:% (post-PEG percentage, separate from concentration).
- salivary_free_testosterone:ng/mL; salivary_cortisone:ng/mL; salivary_17oh_progesterone:ng/mL; salivary_free_progesterone:ng/mL; salivary_androstenedione:ng/mL; salivary_dehydroepiandrosterone:ng/mL; salivary_free_estradiol:pg/mL.
- urine_squamous_epithelial_cells:cells/uL; urine_white_blood_cells:cells/uL; urine_red_blood_cells:cells/uL. No raw alias for cells/uL; explicit operator can version spelling while original remains intact.
- insulin additionally accepts distinct literal family uU/mL, no raw alias and no conversion/merging with uIU/mL. Root may explicitly version source 'мкЕд/мл' to its literal micro-unit label; original retained. No changes to unit synonyms of existing families.

- [ ] RED parameterized normalization synthetic1.25 for eachcanonical/unit ->samevalue/unit. Raw source labels remain as before, including unmapped saliva names and generic urine blood-name canonical behavior; blood WBC/RBC rejectcells/uL. InsulinuU/mLdistinctfromuIU/mL. No auto-recognition newrawsourcealiases.
```python
assert normalize_registered('salivary_free_testosterone','1.25','ng/mL')[1] == 'ng/mL'
assert normalize_registered('urine_white_blood_cells','1.25','cells/uL')[1] == 'cells/uL'
```
- [ ] Implement canonical-onlydefinitions and explicitreadablelabels. Source rawmapping/unit behavior mustremainunchanged foroldPDFs: compare registrycanonical_name/known_unit/normalize outcomes relevantprivateexamplelabels before/after in synthetic tests; canonical-only additions don'tmake oldrawrowseligible.
- [ ] RED exact saved release6e3921cquery compatibility, retaining existing pinned288344bvariants. Add finite pre-reviewed-analytes query version that excludes these newIDs anduU/mL/cells/uL, and pre-partial versions mustexcludeallpostpartialadditions. Activeandretiredownedqueries upgrade safely; editedqueries stillrefused. Test samecardIDs; no vagueSQLnormalization.
- [ ] GREEN focusedregistry/dashboard tests,Ruff/mypychangedfiles; report actualRED/GREEN and commitownedonly. Root performs individualsourcecorrections afterreview, fullsuite+finalreview+liveMetabasereconciliation thenpush.
