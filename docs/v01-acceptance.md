# v0.1 synthetic journey acceptance

Run `uv run pytest tests/test_v01_journeys.py -q` with Docker available. The suite
creates a randomly named disposable PostgreSQL database using the existing guarded
fixture and migrates through `0014_v01_workflow_evidence`. PDFs, vault files, SQLite
Telegram state and synthetic Calendar credentials remain in pytest temporary paths.
No production settings, records, credentials or external services are used. HTTPX
and Requests calls are forbidden, including attempted calls caught inside a service.

## What the eight checks cover

1. **Incoming document → factual overview.** A real, column-major gridded PDF goes
   through `import_document`, source-hashed PDF geometry evidence and explicit
   `approve_observation`. An ambiguous two-number result is not guessed; a separate
   pending result is withheld from factual question evidence and snapshot values.
   Pending-count/data-quality gap signals are allowed. After review, the exact
   dated `5.10 mmol/L`, printed reference and observation/page provenance appear.
   Duplicate import adds no observation. A second profile cannot approve the row
   and its verified lab/report never enter the first profile's overview.
2. **Health question → attributed answer.** The shared
   `HealthQuestionApplicationService` receives a verified lab and a separately
   labelled excerpt from an imported clinical-report PDF. The fake responder uses
   those supplied labels; the service validates them before removing labels and the
   default source footer from display, while retaining explicit non-diagnostic and
   no-prescribed-interval wording. Unknown labels and uncited output
   each fail closed. A source-free question never invokes the fake responder and
   never invents a lab. The JSON input keeps `reported_material` distinct from
   verified evidence.
3. **Documented next action → confirmed reminder → one recurrence.** A reported
   recommendation supplies the reason/source for an explicit repository proposal.
   The date and seven-day recurrence are synthetic user choices, not a medical
   interval inferred from text. Real Telegram command handlers confirm and complete
   it; the real dispatcher/messenger sends to an in-memory Telegram gateway.
   Replayed updates/delivery do not duplicate outbound messages or the next child.
   A due reminder belonging to an unbound second profile cannot reach the first
   person's chat; its dispatch remains a safe failure.
4. **Visit → preparation/answer → opt-in Calendar lifecycle.** Real visit commands
   create one visit despite replay, save preparation questions and an answer, and
   explicitly publish using the real publication service and Calendar adapter with
   fake OAuth/API boundaries. Question addition, move and cancellation reuse one
   stable event identity. At each write, a separate database transaction verifies
   that outgoing questions are already committed. Saved answers enter
   `reported_material`, never verified labs; answers, lab values and archive paths
   do not enter the event. Foreign-profile reads, cancellation and publication fail.
5. **Reviewed lab → Sheets projection and Metabase query.** The real Sheets read
   model and actual unit-specific dashboard SQL execute against the disposable DB.
   Only the approved lab enters history, retaining source value/unit/reference,
   medical date, document/page/observation identity and numeric reference bounds.
   Pending and foreign results remain excluded. Separately stored synthetic WHOOP
   history retains the same profile-bound result before and after lab review.

## Boundaries of this evidence

These are connected application/service journeys, not real Telegram polling,
browser rendering, Google OAuth, Google Sheets writes or live Metabase rendering.
The deterministic answer fake tests context/citation composition, not model
accuracy, clinical reasoning or a guaranteed model-generated disclaimer. The
Calendar fake checks outgoing managed event content, not Google's availability,
permissions or refresh-token behavior. The PDF is digitally generated, not a scan;
OCR/provider quality is outside this suite.

Live connector authorization, final owner Telegram interaction, actual chart/UI
rendering and deployment acceptance are separate final gates. Do not create fake
medical records in a real profile to reproduce these tests. No runtime change or
new provider is part of this acceptance-test package.
