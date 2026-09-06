# Clinical report context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Let free Telegram questions use source-linked doctor conclusions and saved visit answers, without calling unverified text a verified blood result.

**Architecture:** Add a small bounded reported-material channel alongside existing verified observations and health snapshot. Retrieve local source excerpts read-only; retain separate provenance and timestamp meaning. Reuse the existing Yandex/OpenAI-common input builder and citation renderer, without changing model/API or consent configuration.

**Tech Stack:** Existing Python/SQLAlchemy, typed immutable DTOs, JSON prompt blocks, pytest/disposable PostgreSQL.

## Global Constraints

- No external API calls, live medical records, credentials, provider/model/scope changes, database migrations, or writes from retrieval.
- User requested these clinical scenarios already and authorizes autonomous parallel implementation; no new planning/user gate.
- Only the authenticated profile's documents/visits/notes. No filenames, vault paths, full source payloads, or another profile's data in provider input.
- Explicit distinction: verified numeric labs vs quoted source report vs user-recorded visit answer. Neither raw clinical text nor a user answer is a newly verified measurement or established diagnosis.
- Source material is untrusted data, never executable instructions. No automatic prescriptions, reminders or calendar writes from text.
- Keep existing model, citation rules, source count keys and public constructors backward compatible via trailing default fields. Every cited new item appears in both prompt and deterministic source footer; truncated/out-of-budget material cannot be cited.

### Task 1: Bounded clinical excerpts and visit answers in free-question evidence

**Files:** Create `src/health_agent/questions/reports.py`, `tests/questions/test_reports.py`, `docs/clinical-report-context.md`. Modify only `questions/models.py`, `questions/context.py`, `questions/presentation.py`, `questions/openai.py` (shared prompt/input builder, no API behavior change), `questions/service.py` and directly covering question/provider tests. No shared CLI/panel/config/visits models edits.

**Interfaces:** Add frozen `SourceReport(citation_label:str, kind:str, text:str, source_reference:str, medical_date:date|None, recorded_at:datetime)`; kinds exactly `document_excerpt` and `visit_answer`. Add `reports:tuple[SourceReport,...]=()` as trailing default to HealthQuestionContext and QuestionPresentation. `read_reports(session,profile_id,*,as_of:datetime)->tuple[SourceReport,...]` returns max10items, each text≤1400chars, per-kind≤5; public labels `[DOC1]...` and `[VISIT1]...` assigned deterministically. No EvidenceSource enum/source-count migration needed.

- [ ] Step 1: RED tests with distinct synthetic profiles, pending lab rows, document sections and visit answers. Assert imported unverified conclusion appears only in reported_material, never verified_observations; foreign and cancelled-visit data absent; plaintext directions remain quoted JSON data. Concrete payload contract:

```python
context = builder.build(owner, 'Что обсудить с врачом?')
assert context.reports[0].kind == 'document_excerpt'
assert context.reports[0].source_reference.startswith('document:')
assert '[DOC1]' in select_presentation(context).allowed_citations
```

Also test report-only context reaches responder instead of insufficient-data shortcut; fake responder citingDOC1 succeeds with footer, nonexistentDOC99 fails existing citation validator. Existing sleep/weight windows and emergency bypass unchanged. Source report dates never imply an undated import was a medical event today.

- [ ] Step 2: Retrieve document pages through same-profile Document join, latest60 qualifying page records ordered by document.created_at desc/documentID/page, maxoneexcerptperdocument/max5documents. Exact section anchors at physical line start (case-insensitive) `Заключение`, `Рекомендации`, `Диагноз`, `Conclusion`, `Recommendations`, `Assessment`, optionally followed by colon and text. Include exact contiguous source slice beginning at anchor, max1400chars, stop at next recognized section or blank separator/page end; no arbitrary whole-page fallback. Ignore sections without body content. Query may use case-insensitive anchored multiline regex to bound qualifyingpage selection, but revalidate in Python. Stored safe errors specifically indicating unreadable/original-integrity problems exclude document; `no_lab_candidates`/unknown_document alone does not disqualify readable clinical text. Exclude future medical dates; undated reports allowed with medical_date=None and recorded_at explicitly described as local archive time. Do not infer dates, rewrite sections, classify a numeric table as text evidence, or promote pending values. Reference `document:UUID#page=N`.

- [ ] Step 3: Retrieve at most5 recent saved answer notes from profile-joined HealthVisitNote/HealthVisit, kindanswer, visitnotcancelled, note.created_at≤as_of. Text bounded1400 with visible truncation marker; source `visit:UUID#note=UUID`, medical_date from visitstartsdate only if visit alreadyoccurred, otherwiseNone. Record timestamp is note creation, not proof that a clinician said it. Questions/templates must not enter factual answer channel. No mutation/preparation during read.

- [ ] Step 4: Serialize selected reports into a separate `reported_material` list, never `verified_observations`. Extend common safety instructions: quote/attribute report wording, distinguish user notes, cannot establish diagnosis/measurement solely from this channel, embedded instructions ignored, supplied medical_date versus archive timestamp labelled accurately. Keep individual source text bound1400 rather than existing numeric100char clamp. Include only max10 reports selected by presentation. Add citations to allowed set and footer (sourceidentity/kind/date/brief excerpt≤160 chars); return no rawfilepath. Permit report-only answers but retain urgent guard and entire-answer weight limitation. Test actual shared provider input serialization including a malicious quote/citation-label injection and that reports beyondcap cannot be cited.

- [ ] Step 5: Run `uv run pytest tests/questions tests/ai -q`, Ruff changedfiles, mypy src and diff-check. Existing known standaloneflag compatibility fix may be merged as dependency from2781415 if needed; no test expectation relaxation. Commit owned files; report RED/GREEN output and limitations, no real provider calls.
