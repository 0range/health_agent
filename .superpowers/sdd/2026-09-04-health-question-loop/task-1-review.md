# Task 1 independent review

## Verdicts

- **SPEC: FAIL**
- **QUALITY: CHANGES REQUESTED**

The implementation correctly uses parameterized SQLAlchemy retrieval, puts the requested
`profile_id` predicate in every source query, restricts laboratory evidence to
`ReviewStatus.VERIFIED`, uses normalized WHOOP tables rather than raw records, applies
per-source limits, and emits deterministic, display-safe citation data. Those are solid
parts of the Task 1 boundary. The two findings below nevertheless make the evidence
window/date contract materially untrue for recovery and weight/trend questions.

## Findings

### HIGH — recovery is windowed and cited by record-update time, not the physiological recovery date

`HealthContextBuilder._recoveries()` filters, orders, and labels a recovery with
`WhoopRecovery.source_updated_at` (`context.py:170-190`). The normalizer writes this
field from WHOOP's `updated_at` (`whoop/normalize.py:112-132`). WHOOP defines that value
as when the recovery was last updated; recovery belongs to a physiological cycle (and is
associated with a sleep), rather than occurring at its update time. A historical recovery
edited or recomputed today can therefore enter a 14-/30-/90-day answer and be cited as
today's recovery. Conversely an in-window recovery with no `updated_at` is omitted.

Action: retain a physiological timestamp in normalized recovery data (prefer its associated
normalized sleep start/end or cycle start; alternatively retain WHOOP's `created_at` with
accurate semantics), window/order/cite by that timestamp, and add boundary tests for an old
recovery newly updated and a recent recovery whose update time is outside the answer window.
Do not surface `sleep_id`, `cycle_id`, or other external identifiers in the evidence.

### HIGH — `weight_trend` cannot supply a trend and labels sync time as an observation date

`WhoopBodyCurrent` is a current-per-connection record (`whoop/models.py:199-213`), not a
measurement history. Its `observed_at` is assigned from the connector's `fetched_at`
(`whoop/repository.py:150-188`), including when the raw payload is unchanged. Task 1 then
uses this fetched time for the 90-day filter and `[WEIGHT]` observation date
(`context.py:245-271`). Thus a months-old unchanged body value is repeatedly made to look
like a new weight; it also provides at most one current snapshot per connection, so it cannot
support the requested weight/trend evidence despite selecting the 90-day intent window.

Action: either add an immutable, profile-scoped normalized body-measurement history with
the true source/measurement time and query it for the 90-day trend, or explicitly make this
a current-weight-only source: identify its timestamp as sync/as-of time and return an
evidence-insufficient result for trends. Add tests proving that an unchanged old value
cannot masquerade as a recent observation and that a trend has at least two dated snapshots.

### MEDIUM — urgent guard misses common direct emergency wording and has untested alarm false positives

The exact phrase patterns in `safety.py:10-19` omit common direct forms such as "I have
trouble breathing", "chest pressure/tightness", "I feel like killing myself", "I want to
die", Russian "болит грудь", "не хватает воздуха", and inflected Russian forms. At the
same time an informational question such as "What causes chest pain?" always receives the
imperative emergency response. A conservative guard can accept some false positives, but
the present behavior has neither an explicitly chosen policy nor tests beyond one ordinary
sleep question (`tests/questions/test_safety.py:12-28`).

Action: define the intentional sensitivity policy, expand patterns for high-confidence
equivalents (including Russian morphology), and add table-driven positive and negative
coverage. Keep urgent handling local and never pass an urgent question to retrieval or an
external responder.

### LOW — laboratory citation prefers report issue date over specimen collection date

The lab date expression is `coalesce(issued_date, collected_date)` (`context.py:115`). If
both dates exist, an evidence item is dated to when the report was issued, not when the
specimen/observation was collected. That weakens the spec's observation-date provenance
and may alter a window boundary.

Action: use `coalesce(collected_date, issued_date)` unless product requirements explicitly
define report-issue date as the canonical observation date; add a dual-date boundary test.

## Verification and scope

Reviewed the approved design and implementation plan, Task 1 brief/report, the supplied
`9cba3cd..3727cdc` review diff, Task 1 tests, and the current core/WHOOP schemas,
normalizer, and repository. I did not modify implementation and did not run tests.

WHOOP date semantics were checked against the official developer documentation: recovery
`updated_at` is the last update time and recovery is associated with a physiological cycle;
the body-measurement endpoint returns a current measurement object without a measurement
timestamp. See [Recovery](https://developer.whoop.com/docs/developing/user-data/recovery/)
and [WHOOP API reference](https://developer.whoop.com/api/).

## Fix-round re-review (3727cdc..1d3afe0)

### Verdicts

- **SPEC: FAIL**
- **QUALITY: CHANGES REQUESTED**

The two evidence-provenance highs and the laboratory-date low are fixed. Recovery now
uses an outer join constrained by both `profile_id` and `connection_id`, so matching sleep
or cycle data from another profile or connection cannot supply a date. It windows, orders,
and cites the linked sleep/cycle start rather than `source_updated_at`; the new tests cover
both stale-update/recent-physiology directions, cycle fallback, and isolation. The current
body record is accurately identified as `sync_as_of`, and a weight-trend context carries a
stable insufficient-history limitation instead of representing the sync as a measurement.
Laboratory selection now consistently prefers `collected_date` over `issued_date`.

No schema/query regression was found in the changed evidence path: the recovery joins use
only normalized tables and scoped linkage predicates, the resulting external linkage IDs
are not exposed, source caps remain present, laboratory results remain verified and
profile-scoped, and all other WHOOP source queries retain their profile predicates. This
review was static; I did not rerun tests.

### Remaining finding

#### MEDIUM — bare `pressure` still sends ordinary blood-pressure questions to emergency guidance

`_URGENT_PATTERNS` in `src/health_agent/questions/safety.py` includes the standalone
alternative `pressure` in its first English pattern. Consequently a direct but non-urgent
question such as “My blood pressure is 120/80; is that normal?” has a direct-subject match
and returns `URGENT_RESPONSE`. The generic-question prefix exemption does not apply, and
the newly added false-positive tests do not cover this case. This contradicts the stated
high-confidence chest-pressure policy and leaves the prior urgent false-positive finding
unresolved.

Action: remove the bare alternative and require chest context for pressure/tightness (or
use a narrowly tested symptom construction), then add negative coverage for direct
blood-pressure, atmospheric-pressure, and other non-emergency uses while retaining the
intended chest-pressure positives.
