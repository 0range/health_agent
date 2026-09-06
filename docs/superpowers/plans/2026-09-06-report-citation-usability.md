# Report citation usability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Keep original clinical report locators accessible when a real model cites many WHOOP sources in the same answer.
**Architecture:** Deterministic footer orders cited DOC/VISIT before telemetry, retaining bounded detailed references and compact locators for remaining cited clinical reports.
**Tech Stack:** Existing Python questionservice/tests only.

## Global Constraints

- Never broaden allowed citations, inventsourceIDs, promote reportwording to verified facts or alter source selection.
- Keep MAX_RENDERED_REFERENCES=6 detailed entries. At most10 selectedclinicalreports fromexistingpresentation; onlycitedones mayappear.
- No provider/API/model/consent changes, retries, livecalls or healthdatafixtures.
- User alreadyapprovedautonomoususefulv0.1; this fixesactualruntimeusability, no approvalgate.

### Task 1: Preserve cited report locators under footer truncation

Files modify src/health_agent/questions/service.py and tests/questions/test_service.py/test_reports.py only. Root actualquestion had20cites includingDOC1..5, but6telemetryfooters consumedbudget, no document: locator rendered.

- [ ] Failing syntheticmixedcontext test containingvalid DOC1..5andmanyWHOOPcitations, assert all5generatedsource locators appear in footer. Existing uncited/foreign/beyondcap tests remain.
```python
footer = render_source_footer(context, cited_labels)
assert all(report.source_reference in footer for report in selected_cited_reports)
```
- [ ] Order citedreportdetailentries beforeotherreferences. If morethan6citedclinicalreports, appendcompactlines(label+validatedsource_reference only) forremainingclinicalreportswitha bounded10total; no appendedreporttext pastdetailcap. Existingtelemetryhidden-counttext remains truthful andmustnotcountalreadyrenderedcompactclinicalentriesascompletelyomitted. Preserve all existing escaped/displaybound behavior andsafe locator validation.
- [ ] Test mixed20citations,10clinicalreportscompacttail, uncitedclinicalnotshown, invalidgeneratedlocatorsfailclosed, beyondpresentationcapnotshown, no duplicate sourcefooter entries. Run questionservice/report/presentation suites +Ruff/mypy; report/commit. No broadnumericformatting changes now.
