# WHOOP Chart Clarity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Repair the already approved WHOOP dashboard so every chart has a single understandable metric/unit and weight is an honest current snapshot.

**Architecture:** Extend immutable WhoopCardSpec and existing reconciliation, preserving dashboard URLs and existing managed card IDs. Query existing WHOOP tables/views without migrations or changes to stored health data.

**Tech Stack:** Python, SQLAlchemy/PostgreSQL, httpx, local Metabase v0.63.13, pytest.

## Global Constraints

- No OpenAI calls, new dependencies, credentials or real medical values in tests/logs/reports/commits.
- Every query is profile scoped with a validated UUID; never inherit another profile's data.
- Russian user-facing text; source missingness is not zero or healthy. Sync time is not a measurement time.
- No clinical database mutation, migration or change to shared reader grants; preserve unrelated user changes and user-owned Metabase cards.
- Controller alone owns live services and full integration test run; implementer uses synthetic fixtures and focused tests.

---

### Task 1: Metric-specific charts and safe existing-dashboard upgrade

**Files:** Modify `src/health_agent/whoop/dashboard.py`, `tests/whoop/test_dashboard.py`; create `tests/whoop/test_dashboard_queries.py`, `docs/whoop-dashboard.md`. Do not edit `metabase.py`, CLI, shared tests or unrelated modules. Existing fake API from `tests/test_metabase.py` can be reused/subclassed locally.

**Interfaces:** Preserve `whoop_card_specs(profile_id: UUID) -> tuple[WhoopCardSpec, ...]`, `bootstrap_whoop_dashboard(settings, profile_id, *, transport=None, engine=None) -> WhoopDashboardResult`, current CLI output and `_ensure_dashboard_card` interface. Add optional fields to frozen WhoopCardSpec for display, description, unit, legacy names/settings if needed. Keep legacy snapshot specs in one helper for exact ownership comparison instead of duplicating reconciliation blocks.

- [ ] Add failing focused spec tests:

```python
def test_one_metric_per_chart_and_weight_is_not_a_trend():
    specs = whoop_card_specs(PROFILE)
    assert len(specs) == 8
    charts = [s for s in specs if s.display == 'line']
    assert len(charts) == 7
    assert all(len(s.metrics) == 1 for s in charts)
    weight = next(s for s in specs if s.display == 'table')
    assert 'observed_at' in weight.query
    assert 'LIMIT 1' in weight.query
```

Test the exact metric set recovery_score, strain, hrv_rmssd_milli, resting_heart_rate, sleep_hours, sleep_performance_percentage, sleep_efficiency_percentage, weight_kilogram. Assert each title/unit/description describes its own metric and every SQL contains exactly the intended profile. `WhoopCardSpec.display` is required public field for this test; it may default to line for backwards compatibility.

- [ ] Implement single-metric specs. Keep `WHOOP — длительность сна` name with an hours axis; map old `WHOOP — Recovery и strain` to recovery, `WHOOP — HRV и пульс покоя` to HRV, `WHOOP — качество сна` to sleep performance, `WHOOP — вес` to current weight; add strain, RHR and efficiency cards. Each description explains observation-not-diagnosis and no causality; sleep performance means WHOOP sleep need completion, not general sleep quality. Seven line cards expose date plus one metric. Table weight exposes weight_kilogram and observed_at, ordered newest timestamp descending plus stable connection ID, LIMIT 1; description says retrieval time, not weighing time. Do not put graph settings on the table. Use graph.x_axis.title_text / graph.y_axis.title_text for Russian axes, no forced clinical ranges or normal/abnormal colouring.

SQL daily pattern, applied to each independently valid metric:

```sql
SELECT date, metric FROM (
  SELECT DISTINCT ON (c.local_day) c.local_day AS date, c.strain AS metric
  FROM whoop_cycles c
  WHERE c.profile_id = 'validated-UUID' AND c.local_day IS NOT NULL
    AND c.local_day <= (CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::date
    AND c.score_state = 'SCORED' AND c.strain BETWEEN 0 AND 21
  ORDER BY c.local_day, c.start_at DESC,
    c.source_updated_at DESC NULLS LAST, c.id DESC
) selected ORDER BY date
```

Use actual exposed metric aliases instead of metric. Recovery/HRV/RHR join recoveries to cycles on profile_id, connection_id, external_id, using recovery score_state and latest cycle start plus recovery source_updated_at/ID. Sleep queries use sleeps local_day/start_at/source_updated_at/id, score_state SCORED and is_nap=false. Metric validity: percentages 0–100, strain 0–21, HRV/RHR positive and not NaN/infinity, sleep duration positive and at most 24 hours. Weight positive finite. Do not coalesce or synthesize zeros. Descriptions clarify latest valid record/day, not mean, and today's values can update. Reject non-UUID inputs to exported spec builder with TypeError/ValueError rather than interpolating arbitrary strings.

- [ ] Preserve reconciliation. `display`/visualization match desired kind; add description. Before legacy renaming, match old known SQL+profile+visualization/display and collection ownership. Migrate existing five cards in place and attach only missing three. Preserve dashboard ID and existing IDs. No DELETE/archive calls. Keep extra user dashboard cards untouched, position managed cards without overwriting user card layouts; allocate space avoiding occupied user rectangles. Existing clean default names and full UUID suffix for non-default remain. Legacy short UUID suffix ownership must still reject same-prefix foreign profiles. Exercise previous short-name compatibility tests with both old and new shape where appropriate; do not weaken their ownership intent.

- [ ] Add actual disposable-PostgreSQL query tests using existing session fixture, normalize_whoop/store_normalized_record helpers where practical. Execute generated SQL against synthetic rows, not SQL-string assertions alone: two profiles including same external IDs; missing/unscored values excluded; finite guards; future days; primary sleep vs nap; two valid same-day observations return the specified latest exactly once; latest invalid row does not hide valid row; latest weight snapshot selected without producing a series; empty profile returns no rows. Roll back fixtures; never call providers or load production .env.

- [ ] Update API-fake tests to eight cards and non-graph table settings; tests for two calls with stable IDs/counts, old five-card upgrade without duplicates, user card retained, layout repair, same-prefix foreign legacy objects not claimed, invalid UUID rejected. Native queries must remain scoped, not global.

- [ ] Run focused checks, self-review and commit:

```sh
uv run pytest tests/whoop/test_dashboard.py tests/whoop/test_dashboard_queries.py -q
uv run ruff check src/health_agent/whoop/dashboard.py tests/whoop/test_dashboard.py tests/whoop/test_dashboard_queries.py
uv run mypy src/health_agent/whoop/dashboard.py
git diff --check
```

Write full implementation/TDD/test evidence to assigned report, return DONE/concerns, commit and short test result. Document user-facing chart meaning, local URL discovery command, repeatability and limits in docs/whoop-dashboard.md. Reference official docs https://www.metabase.com/docs/latest/questions/visualizations/line-bar-and-area-charts and https://www.metabase.com/docs/latest/questions/visualizations/numbers only for visualization semantics; no live calls by implementer.

## Controller integration checklist

- [ ] Task review, merge and full integration tests once on final code; independent final review.
- [ ] Authenticate existing local Metabase without exposing credentials; read existing managed object metadata before live upgrade; execute each final query, report only status/count/date coverage, not medical values.
- [ ] Verify actual dashboard in browser; no OpenAI requests, no test data in production.
- [ ] Record exact acceptance and outstanding lab-chart/medical/AI work, commit and push working branch only.
