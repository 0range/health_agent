# Local Staging Environment Plan

**Goal:** Run the first live WHOOP acceptance against an isolated, disposable
local environment without touching production state.

**Design:** A dedicated Compose file/project owns separate PostgreSQL and Metabase
databases, ports and named volumes. A small Python staging runner loads only an
explicit staging env file, validates every target against production defaults,
and launches Compose or application commands with a sanitized environment.

The hardening review extended that boundary to effective production overrides,
loopback-only database targets, symlink-safe path creation, and dedicated private
WHOOP credentials; see `2026-09-04-local-staging-hardening.md`.

- [x] Add standalone staging Compose and synthetic `.env.staging.example`.
- [x] Add separate staging vault/temp/token/connector-state paths and ignore local
  staging configuration/state.
- [x] Implement validated start/status/stop/run and guarded clean commands.
- [x] Prevent staging subprocesses from loading or inheriting production `.env`
  values.
- [x] Test port/database/path/credential/Compose-project separation and destructive
  command guards.
- [x] Document mocked → staging → production promotion and WHOOP read-only scope.
- [x] Run full tests, Ruff, mypy, Compose config and an available local smoke; stop
  without deleting volumes.
- [x] Commit, update the local SDD report, and do not push or authorize WHOOP.
