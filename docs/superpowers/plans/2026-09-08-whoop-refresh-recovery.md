# WHOOP refresh recovery

User requested reliable automatic refresh while checking v0.1 release. Existing
refresh succeeded with the stored grant after a sync had permanently marked the
connection reauth_required. Complete the existing OAuth behavior without changing
scopes, schedules, token locking or provider.

- [x] Add a distinct temporary OAuth exception for transport failures, overload,
  unexpected HTTP replies and malformed success payloads. Only explicit OAuth
  rejection may require human authorization.
- [x] Map temporary refresh failure to WhoopApiError, preserving connected state
  and stored token so the next scheduled sync can retry.
- [x] Test expired token → timeout/503/429 → failed sync without lost authorization
  → successful refresh and sync using rotated tokens. Test invalid_grant still
  requires authorization and never prints response bodies or secrets.
- [x] Recover the already latched live state only after verifying the stored
  token's remote profile identity; run real sync, full checks, then release.
