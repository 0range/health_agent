# WHOOP Metabase dashboard — implementation plan

Goal: make already normalized WHOOP data visible in a repeatable local dashboard,
without copying health data outside PostgreSQL/Metabase or mixing profiles.

## Product slice

- `health-agent dashboard setup-whoop --profile-id UUID` provisions a WHOOP
  overview for exactly that profile; the existing blood-dashboard setup remains
  backward compatible.
- The WHOOP dashboard shows daily recovery/strain, HRV/resting HR, sleep duration
  and quality, and weight when WHOOP exposes it.
- Every native query contains an exact validated profile UUID; no cross-profile
  fallback is allowed.
- Setup is idempotent and repairs drifted managed cards/dashboard objects.
- Empty WHOOP data produces empty charts, not fake zero values.

## Implementation

1. Reuse the existing Metabase collection/database reconciler and add immutable
   WHOOP dashboard/card specs without changing the blood dashboard.
2. Add a WHOOP dashboard with small metric-specific cards over the existing
   `whoop_daily_health`, `whoop_sleep_history`, and `whoop_body_snapshot` views.
3. Return and print the exact WHOOP dashboard URL from the CLI.
4. Cover query isolation, idempotency, drift repair, card attachment and CLI output
   with the fake Metabase component tests; then run the complete test/type/migration
   gates and a live local Metabase bootstrap.

Live WHOOP OAuth is intentionally separate: the dashboard can be provisioned while
empty and becomes populated after the first successful sync.
