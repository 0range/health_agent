# WHOOP Metabase dashboard review

Reviewed commits `ecb7848` and `77954f9` as present on `codex/v1-slice-1`
at `dba7e06`. No implementation or live health/OAuth data was changed.

## Verdict

**SPEC verdict: CHANGES.** The card SQL and initial layout meet the metric scope,
but the shortened managed-object key can make one profile's setup take over
another profile's dashboard, and declared drift/legacy reconciliation is
incomplete.

**QUALITY verdict: CHANGES.** Focused tests, WHOOP tests, static checks, CLI help,
and disposable-PostgreSQL query planning pass. The suite misses the reproduced
collision and reconciliation failures, and the planned real local Metabase
bootstrap has no evidence.

**OVERALL verdict: CHANGES.** Do not treat the WHOOP dashboard as multi-profile
ready until findings 1–3 are fixed and covered. Finding 4 is also a required
acceptance gate before claiming compatibility with the pinned Metabase image.

## Findings, ordered by severity

### 1. High — eight-character profile names can cross-wire two profiles

Non-default dashboard and card identity is derived only from
`str(profile_id)[:8]` (`src/health_agent/whoop/dashboard.py:98-100,179`). Two
different UUIDs can legally share that prefix. `_candidate` then selects the
first profile's existing managed objects and the second setup updates their SQL
in place (`src/health_agent/whoop/dashboard.py:151-164,194-207`). Both commands
return the same dashboard ID, and the first profile's saved URL now displays the
second profile's WHOOP queries.

This was reproduced with
`aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa` and
`aaaaaaaa-bbbb-4bbb-8bbb-bbbbbbbbbbbb`: the two results both returned dashboard
ID 1, only five cards remained, and every card query was rewritten to the second
full UUID. The current two-profile test uses different prefixes (`aaaaaaaa` and
`bbbbbbbb`), so it cannot catch this (`tests/whoop/test_dashboard.py:60-82`).

Use a collision-free stable managed identity, such as the complete profile UUID
(a readable profile label can be added separately), and test two UUIDs with the
same first eight characters. Existing ambiguous short-name objects must fail
closed or be migrated only after their embedded exact query UUID identifies the
owner.

### 2. Medium — the naming change duplicates every legacy non-default dashboard

The first commit named all WHOOP objects with the full profile UUID. The follow-up
changed non-default profiles to a short suffix, but `legacy_suffix` is populated
only for the default profile (`src/health_agent/whoop/dashboard.py:98-114,
199-204`). Therefore an already provisioned non-default full-UUID dashboard and
its five cards are not recognized. Re-running setup creates a second dashboard
and five more cards, leaving the prior managed objects and URL behind.

This was reproduced against the fake Metabase component: one legacy dashboard
and five cards became two dashboards and ten cards. Reconcile the old full-UUID
name for every profile, with collision-safe ownership checks, and cover upgrade
from both legacy default and non-default names.

### 3. Medium — setup does not repair managed card layout drift

The product plan explicitly requires repair of drifted managed cards/dashboard
objects (`docs/superpowers/plans/2026-09-04-whoop-metabase-dashboard.md:15,
25-27`). Card definitions are rewritten correctly, and missing attachments get
the intended two-column layout. But `_ensure_dashboard_card` returns as soon as
it sees the card ID and never compares or updates `row`, `col`, `size_x`, or
`size_y` (`src/health_agent/metabase.py:539-546`). Changing the first card's row
to 99 and running setup again left it at 99.

Reconcile the managed attachment's expected position/size while preserving
unmanaged user cards, and add drift tests for both card definition and dashboard
placement. The intended fresh layout itself is coherent: two 12-column cards per
row at rows 0/8, with weight alone at row 16.

### 4. Medium — the required real Metabase bootstrap acceptance has no evidence

The plan requires complete gates and a live local Metabase bootstrap
(`docs/superpowers/plans/2026-09-04-whoop-metabase-dashboard.md:25-27`). The only
dashboard component tests use `httpx.MockTransport` plus `FakeMetabase`, and the
CLI test replaces `bootstrap_whoop_dashboard` entirely
(`tests/whoop/test_dashboard.py:37-120`). No checked-in report or command evidence
shows `dashboard setup-whoop` against the pinned `metabase/metabase:v0.63.13`.

An isolated empty staging smoke is sufficient and needs neither WHOOP OAuth nor
health values. It should run the actual CLI twice, inspect five attached cards
and their positions/queries through the real API, and confirm the returned URL is
reachable. Do not claim live WHOOP data from that smoke.

### 5. Low — CLI reports `ready` for a nonexistent profile and is undocumented

`dashboard setup-whoop` accepts any syntactically valid UUID and immediately
provisions objects; it never verifies a matching `profiles` row
(`src/health_agent/cli.py:280-293`; `src/health_agent/whoop/dashboard.py:89-108`).
A mistyped UUID therefore creates a permanently empty orphan dashboard and prints
`status=ready`. The public README and WHOOP runbook do not mention the command;
only the internal implementation plan does.

Validate profile existence before provisioning, test the failure path, and add
the command/open-URL workflow to the operator runbook.

## Verified behavior

- All five native queries contain the complete typed profile UUID and no
  cross-profile fallback.
- Every query suppresses absent measurements instead of replacing them with
  zero; the weight view remains a current snapshot rather than invented history.
- The query columns exactly match `whoop_daily_health`,
  `whoop_sleep_history`, and `whoop_body_snapshot`. All five queries passed
  PostgreSQL `EXPLAIN` after migration to `0005_whoop` in a freshly created,
  empty disposable database.
- Fresh provisioning creates five line cards with the expected date dimension,
  metric columns, two-column layout, profile-bound SQL, and exact dashboard URL.
- Repeated setup for one non-colliding profile does not create duplicates and
  card definition drift is overwritten. Default-profile visible names are clean,
  and the old default full-UUID objects are recognized.
- Existing blood-dashboard behavior is unchanged by the optional layout
  arguments.

## Independently reproduced gates

- `uv run pytest -q tests/whoop/test_dashboard.py tests/test_metabase.py`:
  **18 passed** (five PyMuPDF SWIG deprecation warnings).
- `uv run pytest -q tests/whoop`: **95 passed** (same five warnings).
- `uv run ruff check` on the changed dashboard/Metabase/CLI/tests: **passed**.
- `uv run mypy src`: **passed**, 55 source files.
- `uv run health-agent dashboard setup-whoop --help`: **passed**.
- `git diff --check ecb7848^..77954f9`: **passed**.
- Disposable PostgreSQL migration plus `EXPLAIN` for all five exact card queries:
  **passed**; the guarded temporary container was removed afterward.

No real WHOOP token, payload, health value, production database, or live
Metabase object was read or modified during this review.

## Fix-round re-review — 2026-09-04 (`40f6424`)

**SPEC verdict: CHANGES.** Clean-state full-UUID isolation, layout repair,
unknown-profile rejection, and operator documentation are fixed. One
cross-profile legacy ownership path remains.

**QUALITY verdict: CHANGES.** The focused gates and the supplied pinned-Metabase
smoke are green, but the legacy test covers only a matching owner and misses the
reproduced same-prefix mismatched-owner takeover.

**OVERALL verdict: CHANGES.** Fix the single blocker below before declaring the
dashboard safe for an installation that may have run the short-name version.

### Remaining blocker — High: legacy short names are claimed without proving ownership

New non-default objects now use the complete UUID, so two clean profiles with the
same first eight characters are isolated. During upgrade, however, both
`_ensure_named_dashboard` and `_ensure_whoop_card` accept the old eight-character
name solely by name and collection; they do not verify the full UUID already
embedded in the legacy cards' SQL (`src/health_agent/whoop/dashboard.py:98-128,
151-164,194-207`).

This leaves a concrete cross-profile transition:

1. legacy dashboard `[aaaaaaaa]` and its cards contain exact profile-A SQL;
2. a different profile B with the same `aaaaaaaa` prefix runs setup first after
   upgrade;
3. B receives the same dashboard ID, and all five cards are renamed and rewritten
   from A to B.

The saved profile-A dashboard URL consequently shows profile-B data. This was
reproduced against the component fake: dashboard ID remained 1, dashboard/card
counts remained 1/5, all five queries changed to B, and no A query remained. The
new legacy test manually shortens objects whose SQL already belongs to the same
requested profile, so it cannot detect this case
(`tests/whoop/test_dashboard.py:113-137`).

Reuse a short-name legacy set only after its attached managed cards prove the
requested complete UUID. If the name belongs to another UUID, create the new
full-UUID set or fail closed; do not rename/rewrite the foreign set. Add the
same-prefix, mismatched-owner upgrade test.

### Findings closed

- **Clean full-UUID isolation:** closed. Non-default managed names use the whole
  UUID, and the new same-prefix clean-state test gets two dashboards and ten
  correctly bound cards.
- **Ordinary non-default legacy reuse:** partially closed, subject only to the
  ownership blocker above. A matching short-name set is renamed in place without
  duplicates; the original full-UUID naming is already the desired name.
- **Layout drift:** closed. Existing attachments have row/column/size compared
  and repaired while unrelated dashcards are retained.
- **Unknown profile and runbook:** closed. The CLI checks PostgreSQL before any
  Metabase provisioning, returns a parameter error for an absent profile, and
  the WHOOP runbook documents setup/open behavior and full-UUID isolation.
- **Pinned Metabase acceptance:** closed. The supplied empty local bootstrap was
  independently inspected read-only: the running image is
  `metabase/metabase:v0.63.13`, `/api/health` is `ok`, dashboard ID 3 has the
  expected name and exactly five attached line cards, positions are
  `(0,0)`, `(0,12)`, `(8,0)`, `(8,12)`, `(16,0)` with size `12x8`, all five card
  definitions contain the exact default profile UUID, and none contains
  `COALESCE`. No card result or health value was queried.

### Reproduced gates

- `uv run pytest -q tests/whoop/test_dashboard.py tests/test_metabase.py`:
  **22 passed** (five existing PyMuPDF SWIG deprecation warnings).
- `uv run pytest -q tests/whoop`: **99 passed** (same warnings).
- Focused Ruff: **passed**.
- `uv run mypy src`: **passed**, 55 source files.
- `health-agent dashboard setup-whoop --help`: **passed**.
- Fix diff check: **passed**.
- Read-only pinned-Metabase health, dashboard/card count, layout, display, exact
  profile-filter, and no-`COALESCE` checks: **passed**.

No implementation, OAuth state, Metabase object, profile row, or health value was
modified during this re-review.

## Final ownership re-review — 2026-09-04 (`74f29c4`)

**SPEC verdict: SHIP.** The remaining legacy ownership blocker is closed without
regressing clean full-UUID isolation, matching-owner migration, layout repair,
profile validation, or empty-chart semantics.

**QUALITY verdict: SHIP.** The focused and complete WHOOP gates pass, both legacy
ownership branches were independently reproduced, and the existing pinned
Metabase dashboard remains healthy and correctly attached.

**OVERALL verdict: SHIP.** The WHOOP Metabase dashboard is ready to merge/use for
the approved local multi-profile scope.

### Blocker closure

`_legacy_objects_owned_by` now permits reuse of an ambiguous eight-character
legacy name only when all five expected cards exist exactly once in the managed
collection, contain the requested profile's exact complete SQL, have the expected
managed visualization contract, and are exactly the cards attached to the legacy
dashboard. Otherwise both dashboard and card legacy fallbacks are disabled and a
new full-UUID object set is created.

Independent reproduction verified both required paths:

- A legacy `[aaaaaaaa]` set containing profile-A SQL could not be claimed by
  same-prefix profile B. The dashboard IDs differed, A's five queries remained
  unchanged, and B received five separate full-UUID cards (2 dashboards, 10
  cards total).
- The same matching owner reused its legacy dashboard ID, renamed the set to the
  complete UUID, and retained exactly one dashboard with five cards.

The new regression test covers the foreign-owner case in addition to the
matching-owner and clean same-prefix tests. Ownership proof is intentionally
conservative: an incomplete or unprovable legacy set is left untouched rather
than being rewritten.

### Final gates

- `uv run pytest -q tests/whoop/test_dashboard.py tests/test_metabase.py`:
  **23 passed** (five existing PyMuPDF SWIG deprecation warnings).
- `uv run pytest -q tests/whoop`: **100 passed** (same warnings).
- Focused Ruff and `uv run mypy src`: **passed** (55 source files).
- `git diff --check 40f6424..74f29c4`: **passed**.
- Read-only live evidence reconfirmed: image
  `metabase/metabase:v0.63.13`, health `ok`, dashboard ID 3 with five line cards,
  expected `2+2+1` positions and `12x8` size, five exact default-profile filters,
  and zero `COALESCE` cards.

No health result, OAuth state, database row, implementation file, or Metabase
object was read beyond content-free dashboard metadata or modified.
