# Local staging hardening plan

Goal: close every finding from the staging review before any live WHOOP OAuth.

1. Canonicalize the effective staging PostgreSQL target, require loopback, and compare ports, database identities, roles, and managed paths against effective production overrides from `.env`.
2. Treat `.staging` as a filesystem trust boundary: reject symlinks or escapes in every existing component and create private directories with `dir_fd`/`O_NOFOLLOW` and mode `0700`.
3. Reject inline WHOOP credentials, fix the credential location under `.staging`, validate regular-file/mode `0600`, and require it only for WHOOP auth/sync commands.
4. Fix the Metabase application database name to `metabase_staging` so Compose and initialization cannot diverge.
5. Cover every invariant with isolated tests, update the runbook, then run Ruff, mypy, the full test suite, migration checks, and a real isolated Compose smoke without credentials or OAuth.

Re-review addendum: production Compose now also needs an explicit project identity;
its fixed ports/database/role must remain forbidden even when application settings
override them, and filesystem collision checks must reject ancestor/descendant
overlap in either direction rather than exact equality only.
