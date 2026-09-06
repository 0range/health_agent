# Lab dashboard runtime acceptance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Correct a real Metabase duplicate-date rendering defect and expose useful per-profile lab dashboards.
**Architecture:** Keep existing Metabase/native SQL; categorical chronological unique result labels prevent frontend aggregation, source dates remain unchanged. Show available series only, with clear empty-state detail table. Existing strict ownership guards get an explicit exact-previous-version migration path.
**Tech Stack:** Existing Python/SQLAlchemy/Metabase/panel.

## Global Constraints

- No synthetic production data or live credentials in logs. Root handles production provisioning after review.
- Never combine distinct same-day measurements or source-unit families. No artificial timestamps, averaging, sum, silently dropping duplicates, or invented reference limits.
- Existing owned/user dashboard cards must not be overwritten merely by matching name/marker. No deleting user cards. Existing WHOOP remains intact.
- No heavy new chart frontend or framework. User explicitly wants basic polish and useful data, not empty chart walls.
- Calendar task owns other CLI/config/panel sections; coordinate narrow edits only. No schema edits.

### Task 1: Fix observed rendering and wire profile destinations

**Files:** modify src/health_agent/lab_dashboard.py, existing dashboard CLI functions and narrowly panel destination composition/http URL allowlist; create focused tests/docs as needed. Do not edit medical workflow forms/Calendar code.

**Observed evidence:** root staged real Metabase0.63.13 dashboard3 at127.0.0.1:54000, synthetic profile00000000-0000-0000-0000-000000009901, two ferritin rows30/40 andsameprintedref10-100on2020-01-01. SQL preserved both, frontend line chart SUMMED into70 andrefs20/200! Screenshot ignored output/playwright/lab-staging.png proves200ref. Staging has5syntheticrows includingoneug/L999; no actual health values. Existing dash has15cards41..55 including13emptydefaults. Geometry merged after initialprovision addedregistryalias causing exactSQL collision onrerun; need known-old migration, not bypass.

**Interfaces:** retain bootstrap_lab_dashboard/discover_lab_series; add CLI `dashboard setup-labs --profile-id UUID` (or make existingsetup use it compatibly) and save local profile-bound dashboard destination through existing/private atomic store. Root can accept a new small `data/dashboards/<profile>.json` map containing ONLY lab/WHOOP IDs and origin, never credentials. Provide optionalroot explicit default alignedstaging, or use settings existing connector_state_root / dashboards to inheritisolation. Panel GET reads map only; no Metabase calls/writes onGET. Setup-whoop saves analogous destination. Existingfallback Metabase landinglinkmayremain alongside two directcards.

- [ ] Step1: regression SQL test proves distinct `date_label` for same-day rows with unchanged result30/40/reflow10/refhigh100. Keep rawdatecolumn for provenance. Choose ordered categorical labels `YYYY-MM-DD` and suffix` · 1`,` · 2` only when repeated, stable row_number bydocument/page/id; chart dimension date_label andordinalscale. This represents separate observations honestly without fabricating collectiontimes. Tooltip/table keeps actualdate. Assert mappings not summed andstrictunitseparation.

```python
assert [r.result for r in rows] == [Decimal(30), Decimal(40)]
assert len({r.date_label for r in rows}) == 2
assert {r.reference_high for r in rows} == {Decimal(100)}
```

- [ ] Step2: implement chart SQL/settings; mark x-axis `Дата · отдельные измерения`, Russianresult/reference legends and human labels for table columns/comparison. Discover only actually available registeredseries ordered withDEFAULT_SERIESpriority, not13emptycards. Detailtableemptydescription tellsuser noverifieddatedresults yet; no falselyhealthyzerocards. Preserve sourceproof table but hide noisyinternalcolumnsbydefault ifsupported (stillaccessible). AvoiddisplayfullUUID in visibleheading when ownershipidentitycanremainstoredname via dashboardcard visualizationtitle safely; rootacceptsmallresidualUUID ifAPIcan'tsupportwithoutcomplexity.
- [ ] Step3: exact-owned migration from currentoldquery template to correctedtemplate, including narrow registryexpansion. Oldtemplate from0e0357c/78126e4 mustmatchfullknownSQLstructure/profile/database/collection; don'tacceptarbitraryeditedSQL withsameprefix. Use explicittemplate builder/allowedoldregistryentries or equivalent narrow knownoldsignature. Refusecustompredicates/foreignprofile/databases. No registrywidehandmaintainedcopy exceptnamedlegacyversionneededformigration. Update tests ensureoldownedcardupdates anduserSQL remainsuntouched/failsclosed.
- [ ] Step4: wire CLI/profilevalidator/private destinationrecord and panel directlinks lab/WHOOP. SnapshotlinkincludesprofileUUID recordcheck andlocalMetabaseoriginvalidation; invalidrecordrendersunavailable withouttoken/pathleak. Repeatedsetup idempotent, wrongprofilereturns404/CLIinvalid beforewrites. Do not generate dashboard onpanelGET.
- [ ] Step5: run labdashboard/Metabase/CLI/panel tests plus Ruff/mypy; write report/commit. Root then provisions only stagingexistingprofile and usesPlaywrightbrowser to verify30/40andref100not200 inactualfrontend, beforeproduction. No need to run browser in yourworktree orduplicate fullsuite.
