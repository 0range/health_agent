# Local staging hardening report

Status: ready for re-review; no live credentials or OAuth were used.

## Review findings closed

- The effective application PostgreSQL target is canonicalized and required to
  use loopback whether supplied as fields or `DATABASE_URL`.
- Staging ports, database names, roles, and all managed filesystem targets are
  compared against the effective production `.env` plus process overrides; those
  production values are never exported to a staging child.
- `.staging`, managed roots, connector paths, and credential paths reject every
  existing symlink component and lexical escape. Private directories are created
  through `dir_fd` plus `O_NOFOLLOW` and chmodded to `0700`, including the top
  `.staging` directory.
- Inline `WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET` are forbidden. WHOOP auth and
  sync require a dedicated regular non-symlink staging credentials file at mode
  `0600`; status remains usable while disconnected. A staging env containing a
  non-synthetic database secret must itself be a regular `0600` file.
- The Metabase application database is fixed to `metabase_staging` in both
  Compose and init SQL; `STAGING_METABASE_DB` is rejected and the application DB
  cannot reuse that name.
- The staging config now isolates the merged Gmail and Telegram filesystem
  endpoints as well as WHOOP, vault, temp, and connector state.
- The CLI no longer resolves an explicit env-file symlink before validation.

## Automated gates

- Focused staging suite: 37 passed.
- Full suite: 317 passed (only pre-existing SWIG deprecation warnings).
- Ruff: passed.
- mypy over `src`: passed.
- `git diff --check`: passed.
- Compose configuration validation: passed.

## Real isolated smoke

The synthetic staging volumes from the earlier infrastructure smoke were removed
only through the exact guarded `health-agent-staging` project command, then
recreated. Production volumes and `.staging` files were not targeted.

- PostgreSQL: `127.0.0.1:56432`, database/user
  `health_agent_staging`; Alembic reached `0005_whoop (head)`.
- WHOOP status without credentials: `configured=false`, `token=missing`, no live
  payloads.
- Metabase: `127.0.0.1:54000`, dashboard bootstrap succeeded and `/api/health`
  returned `ok`.
- Actual modes for `.staging`, vault, temp, token, connector-state, and credentials
  directories were all `0700`.
- Final state: both staging containers stopped; both staging-only volumes retained.

Branch was rebased onto foundation `509309c`. The review report commit was
preserved as rebased commit `0988b67`. No push was performed.
