# v0.1 final journeys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Make daily medical actions readable and verify the five approved medical journeys as an integrated product.
**Architecture:** Keep existing server-rendered panel and independent synthetic PostgreSQL. Two isolated tasks: small UIpolish and journey acceptance tests, with no new services/providers.
**Tech Stack:** Existing Python/HTML/SQLAlchemy/pytest/Telegram/Calendar adapters.

## Global Constraints

- No real-profile test visits/reminders/documents; tests use disposable PostgreSQL and fake gateways exclusively.
- Preserve profile isolation, source provenance, CSRF/origin/body limits, explicit Calendaropt-in and reminderconfirmation.
- No new provider/model/library, frontendframework or productfeature. User approvedbasicpleasantv0.1 andautonomousparallelcompletion.
- Current combined schema head0014_v01_workflow_evidence; do not add migrations or change sourcecontracts.
- Root handles live verification/deployment. Do not ask owner for checks now.

### Task 1: Compact accessible medical actions

Files modify `src/health_agent/panel/http.py` medicalrenderonly (or focused new renderinghelper), tests/panel/test_workflows.py or new tests/panel/test_medical_polish.py. Do not alter dashboardURLhelper, service/repository/Calendarlogic.

- [ ] Add failing renderingtests emptyprofilecontains only two createforms, no empty requiredselects; populatedprofilecontains allapplicableactions but grouped/collapsed not giantwall. Formcontrols have associatedlabelsviawrapping oruniqueid/for, no duplicatedIDs, errors/notices readable, all source/usertextescaped.
```python
assert 'name="code"' not in empty_html
assert empty_html.count('<form ') == 2
```
- [ ] Preserve two clearsections «Визиты» and «Напоминания», currentstatuslists, createforms. Put secondaryexistingitemactions in `<details><summary>` groupedbyvisit/reminder, onlywhen eligibleitems exist. Each actionselectshows human-readabletitle/time, no manualcodeentry. Use correctapplicablestatussets from existingrepository; cancelledvisitsnotofferednewquestions/moves. Keep all IDs/action_id/CSRFhidden values and samePOSToperations.
- [ ] Russian Calendarconnection copy shouldnotexposerawnot_configured/oauth_requiredtokens inprimarytext; ifonlyrenderedstringavailabletranslateexactprefixeswithoutmaskingerrors. Provide concise timezonehint «Время — Москва». Mobile layout mustfit390px; no CSSframework/assets.
- [ ] Run focusedpanel/workflow tests Ruff/mypy; commit/report. Root actualbrowser atdesktop/mobile andsyntheticPOST onlyafterreview.

### Task 2: Cross-module five-journey acceptance

Files create `tests/test_v01_journeys.py` and `docs/v01-acceptance.md`; test-only productionchangesnotallowed. Reusepublicconstructors/currentfake helpers wherepossible withoutcopyinglargehelperframework. Ownnosharedruntimefiles.

- [ ] Build synthetic actualgriddedPDF in temporaryvault -> normalimporter -> newpendingPDFproof rows -> verifyoneexplicitlywithapprove_observation. AssertpendingexcludedfromHealthContextBuilder/HealthSnapshot beforeapproval; verifieddatedresult sourcevalue/unit/ref appearsafterreview; exactduplicateimportnoextraobservation; nootherprofileevidence. This covers incomingfile and «что происходит» factualdata.
- [ ] Fake responder mustreceivebothverifiedlab andseparateattributedclinicalexcerpt, respondwithvalidsuppliedlabels. Invoke sharedHealthQuestionApplicationService usedbyTelegram; finalanswerincludestraceablesources anddisclaimer/limitations whereapplicable. Testunknownoruncitedlabel failsclosed; source-freequestion noinventedlab. No realAIcall.
- [ ] User «что сделать/проверить» representedbydocumentedrecommendation plusproposedreminder, notautomaticmedicalinterval. Create explicitreminder via actualTelegramhandler orrepository, confirm then due-dispatchfakeTelegram gateway, completeonce->onenextrecurrence; duplicateupdate no duplicateoutbound/child; otherpersonnotnotified.
- [ ] Createvisit throughDatabaseVisitCommands; preparequestions, appendanswer. ExplicitpublishtofakeCalendar usingactualCalendarPublicationService/adapterifpractical, theneditquestion/move/cancel, assert stableeventidentity andcommittedquestionnotes, noanswer/privatehealthdatainevent. Savedanswer joinsreported_materialnotverifiedlabs; otherprofilecannotaccess/actions.
- [ ] Verify verifiedlab iseligibleforexistingSheets projection/readmodel and Metabaseunit-specificquery withsource/reference; ambiguouspendingexcluded. Use realqueryonfixtureDB/fakeSheetsAPI ifavailable. SeparateWHOOPhistory remainsintact. No externalnetwork; describe exactcoverageandwhatrequiresownerfinalOAuth/Telegraminteraction honestly.
- [ ] Run `uv run pytest tests/test_v01_journeys.py -q` plus reusedhelpersuitesonlyasneeded, Ruff. Report exacttests/howfakeslimitlivecoverage; commit. Rootfullsuite andliveconnectorchecks remainseparategate.
