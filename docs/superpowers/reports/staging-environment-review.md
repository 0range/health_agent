# Local staging environment review

Review target: `codex/v1-staging` at `568b22d`, relative to `c112885`.

## Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

The checked-in defaults and the recorded smoke use a genuinely separate Compose
project, containers, loopback ports, databases, volumes, and local data paths.
However, the validator is not yet fail-closed for every configuration it accepts.
Findings 1 and 2 are blockers before staging is used for live WHOOP OAuth or user
health data.

## Findings

### 1. High — an accepted `POSTGRES_HOST` can send staging migrations and application commands to a remote database

`POSTGRES_HOST` is required and passed to every application subprocess, but
`StagingEnvironment.validate()` never checks it
(`src/health_agent/staging.py:73-118`). When `DATABASE_URL` is omitted—as it is in
the supplied example—`Settings` constructs its URL directly from
`POSTGRES_HOST`, port, database, role, and password
(`src/health_agent/config.py:149-156`). Therefore an env file with, for example,
`POSTGRES_HOST=database.example` and otherwise accepted staging-looking values
passes validation; `staging start` starts the local container and then runs
`alembic upgrade head` against the remote host.

This violates both “local staging” and fail-closed production isolation. Require
the effective application database host to be loopback, whether it comes from
individual PostgreSQL fields or `DATABASE_URL`, and test the no-`DATABASE_URL`
remote-host case. Ideally derive and validate one canonical effective URL rather
than validating the two representations separately.

**Impact:** a typo or copied configuration can migrate/write a non-staging
database while the command is labelled `staging`.

### 2. High — a symlinked `.staging` root defeats every path-containment check

Both the trust boundary and candidate paths are resolved through symlinks before
containment is evaluated (`src/health_agent/staging.py:119-153`). If `.staging`
itself is a symlink, `staging_root` becomes the symlink target and all paths below
that target appear valid. A direct example is `.staging -> data`: the default
`.staging/vault` and `.staging/tmp` then resolve to the production-default
`data/vault` and `data/tmp` and pass validation. `prepare_local_roots()` also uses
the resolved root and will chmod/create objects there
(`src/health_agent/staging.py:180-197`). A broader symlink target plus permitted
overrides can similarly overlap production token or connector-state paths.

Reject a symlink at `.staging` and every existing component from the repository
root through each managed data/credential path, then create/open directories
without following a replaced final component. Add tests for a symlinked staging
root and nested symlinked components.

**Impact:** staging imports or OAuth can read/write production files despite all
current path checks reporting the configuration isolated.

### 3. Medium — isolation is checked against hard-coded defaults, not the installation's effective production targets

Ports, database names, and role are compared only with constants
(`src/health_agent/staging.py:11-14,78-98`); filesystem checks assume the listed
default production paths. The production `.env` is correctly not loaded into a
staging subprocess, but it is also never inspected solely for collision
detection. Thus a future legitimate production override can choose the same
non-default port, database identity, or `.staging`-contained path and remain
accepted by staging. On this Mac the current production `.env` contains only a
password override, so the observed configuration still uses the documented
production defaults and does not currently collide.

For a durable fail-closed boundary, parse only the production target keys from
the effective production config, compare canonical endpoints and filesystem
identities, and never export those production values to the staging child.
Alternatively make non-default production targets explicitly unsupported and
enforce that invariant in production commands.

### 4. Medium — staging permits WHOOP secrets in a normally mode-0644 env file and bypasses the dedicated credential-file policy

The parser accepts arbitrary uppercase keys, including optional
`WHOOP_CLIENT_ID` and `WHOOP_CLIENT_SECRET`, and merges them into the child
environment (`src/health_agent/staging.py:155-162,243-276`). `Settings` gives that
pair priority over `WHOOP_CLIENT_CREDENTIALS_FILE`
(`src/health_agent/config.py:108-117`). Consequently a user can put WHOOP secrets
in `.env.staging`; the file's mode is not checked or hardened, and the documented
separate mode-0600 credential file is silently bypassed. `.gitignore` prevents an
ordinary commit, but not same-machine disclosure or accidental process-environment
exposure.

Reject the two inline secret keys in staging and require the validated file path.
Also require an existing staging env containing any non-synthetic secret to be a
regular non-symlink file with mode `0600`.

### 5. Medium — `STAGING_METABASE_DB` is presented as configurable but initialization is hard-coded

The validator and Compose file accept any non-production
`STAGING_METABASE_DB`, while the only init script always executes
`CREATE DATABASE metabase_staging;`
(`compose.staging.yaml:27-32`,
`docker/postgres/staging-init/001-metabase.sql:1`). An accepted override therefore
causes Metabase startup to fail because its selected database was never created.
Likewise, setting it equal to `POSTGRES_DB` is not rejected and makes the fixed
init fail on a fresh volume.

Either fix this database name as an invariant (as the Compose project already is)
or use an init mechanism that safely consumes and quotes the validated name. Add
Compose-level tests for the supported override policy and require the application
and Metabase database names to differ.

### 6. Low — the retained smoke tree does not match the claimed top-level directory mode

The implementation test asserts a mode-0700 `.staging` root, and the code calls
`chmod(0o700)`. Read-only inspection after the recorded smoke found the retained
worktree `.staging` itself at `0755`; its `vault`, `tmp`, `tokens`, and
`connector-state` children were correctly `0700`, and WHOOP lock files were
`0600`. This does not currently expose data contents because the sensitive child
directories remain private, but the discrepancy means the real smoke did not
prove the exact root-mode claim. Record and assert actual modes as part of the
smoke handoff, including any step that recreates the top-level directory.

## Confirmed behavior

- The default Compose topology is isolated: project/name
  `health-agent-staging`; PostgreSQL at `127.0.0.1:56432`; Metabase at
  `127.0.0.1:54000`; separate `health_agent_staging` and `metabase_staging`
  databases; and project-prefixed PostgreSQL/Metabase volumes. Both published
  ports are loopback-only.
- `compose_command()` pins project name, env file, and the absolute staging
  Compose file. Ambient managed database, Metabase, path, WHOOP token, and WHOOP
  credential variables are removed before staging values are installed. The
  child receives `HEALTH_AGENT_ENV_FILE`, so application/Alembic processes do not
  load production `.env`.
- `start` prepares roots, waits for Compose health, and migrates only after the
  services start. It prints success only after migration. `status` and `stop`
  target only the pinned project; `stop` does not remove volumes. `run` rejects an
  empty command and executes argument arrays without a shell.
- `clean` requires the exact literal `health-agent-staging` and performs
  `down --volumes --remove-orphans` only for the pinned project/file. It does not
  remove `.staging` files. This is proportionate to the intended local workflow.
- The default WHOOP credential path is separate from production; final-file
  symlinks and modes other than `0600` are rejected later by `Settings`. The
  default token root is staging-specific, and inherited WHOOP ID/secret/token-root
  variables are scrubbed.
- Automated evidence is coherent but was not rerun for this review. The SDD report
  records 166 full-suite passes, 15 focused passes, Ruff, mypy, Compose config,
  migrations through `0005_whoop`, WHOOP disconnected status, Metabase health and
  dashboard bootstrap.

## Independent smoke-artifact inspection

No services were started or tests rerun during review. Read-only Docker inspection
confirmed that the retained containers are stopped, are labelled with project
`health-agent-staging`, and were created from this worktree's absolute
`compose.staging.yaml` plus `.env.staging.example`. Their bindings are exactly
`127.0.0.1:56432 -> 5432` and `127.0.0.1:54000 -> 3000`; mounts reference only the
staging init directory and these volumes:

- `health-agent-staging_staging_health_postgres`
- `health-agent-staging_staging_health_metabase`

Both volumes remain present, consistent with the reported final `stop`. The main
installation's current `.env` was inspected by key name only and contains only
`POSTGRES_PASSWORD`, so its effective targets remain the documented defaults.
No credentials, token contents, database rows, or health payloads were read.

## Documentation and test gaps

- The runbook is concise and truthful for the default happy path, including
  mocked → staging → production promotion and the fact that staging OAuth must be
  redone for production. It should stop promising that every accepted override is
  isolated until findings 1–5 are closed.
- Existing tests are isolated and runner-injected, but they do not cover remote
  `POSTGRES_HOST`, symlinked `.staging`/nested components, inline WHOOP secrets,
  actual production overrides, Metabase DB overrides/equality, env-file mode, or
  the complete CLI `run -- ...` parsing path.
- Container images use exact version tags but not immutable digests. This is
  acceptable for the local non-enterprise staging scope, though recording digests
  would make later acceptance runs more reproducible.

## Review method

Read the plan, runbook, Compose files, env example, staging/config/CLI code,
WHOOP credential/token boundaries, tests, branch diff, and external SDD smoke
report. Inspected retained Docker metadata and local file modes read-only. No
implementation was modified, no tests or services were run, no volume was deleted,
and no secret value was displayed. The security-best-practices checklist informed
the fail-closed path, secret-handling, and subprocess-boundary review; no
framework-specific reference existed for this Python CLI/Compose stack.
