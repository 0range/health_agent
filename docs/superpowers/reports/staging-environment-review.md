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

## Fix re-review — 2026-09-04 (`4a21e5c`)

### Verdict

- **SPEC: CHANGES**
- **QUALITY: CHANGES**
- **OVERALL: CHANGES**

The default staging configuration now passes a real isolated smoke and five of
the six original findings are closed. Original finding 3 remains a blocker: the
production collision model still misses containment relationships and can forget
the repository's fixed production Compose targets when application overrides are
present. The Compose project name is also not included in the isolation model.

### Remaining blockers, ordered by severity

#### 1. High — the staging and production Compose projects can be the same

Staging pins `health-agent-staging` in the Compose file, command line, and child
environment (`compose.staging.yaml:1`; `src/health_agent/staging.py:12,319-339`).
That is internally consistent, but neither `ProductionTargets` nor validation
models the effective production Compose project. The production Compose file has
no fixed `name:` (`compose.yaml:1`), so its default is the checkout directory or
an external `COMPOSE_PROJECT_NAME`.

This is not hypothetical in the reviewed worktree: independent
`docker compose --file compose.yaml config` resolved the production file to
`name: health-agent-staging`. Both files also use service names `postgres` and
`metabase`. Starting either definition under the same project can therefore
recreate the other's containers; guarded `staging clean --remove-orphans` can
stop/remove containers from that shared project. Different logical volume names
reduce data deletion risk but do not prevent production replacement or downtime.

Use an explicit, immutable production project name distinct from staging (the
simplest option), or derive and reject every effective production project source
before any `up`, `stop`, or `down`. Add an acceptance test that renders both
Compose files and asserts distinct project identities as well as distinct
containers/volumes.

The actual smoke during this review did **not** hit this collision: the running
production installation was project `health-agent`, while staging was
`health-agent-staging`. That confirms the happy path, not the fail-closed invariant.

#### 2. High — production overrides replace fixed Compose collision targets instead of augmenting them

The production Compose topology is fixed at PostgreSQL `55432`, Metabase `53000`,
database/role `health_agent`, and Metabase DB `metabase`
(`compose.yaml:5-9,22-32`). `ProductionTargets.load`, however, replaces these
defaults with `POSTGRES_*`/`METABASE_URL` values from the production application
environment (`src/health_agent/staging.py:123-170`). It does not always union the
fixed Compose targets. `PRODUCTION_DATABASES` is declared but is not used.

For example, if production `.env` sets `POSTGRES_PORT=56433` and
`METABASE_URL=http://127.0.0.1:54001`, staging can be configured for `55432` and
`53000`; validation accepts them even though a running production Compose still
owns those fixed ports. The same omission exists for the fixed `health_agent`
database/role after corresponding application overrides. A direct diagnostic
against the current validator confirmed that the fixed `55432`/`53000` pair is
accepted when the modeled production application ports are overridden.

Always retain the fixed production Compose ports, database names, roles, and
project in the forbidden set, then add effective application targets on top.
Tests currently prove only the inverse case—production is overridden *to the
staging defaults* (`tests/test_staging.py:134-158`)—and miss an override *away
from production Compose defaults* followed by staging reuse of those defaults.

#### 3. High — production path overlap is equality-only, not containment-safe

Every staging path must be below `.staging`, and symlink-safe lexical creation is
now robust. But comparison to effective production paths is only
`identity in production.paths` (`src/health_agent/staging.py:273-286`). It does
not reject either side containing the other.

Both of these production configurations were independently accepted by the
current validator:

- production `VAULT_ROOT=.staging`, which contains every staging target;
- production `VAULT_ROOT=.staging/vault/production`, which is inside the staging
  vault root.

In either case staging can write or chmod within a production-owned tree. Compare
canonical identities for equality **and** ancestor/descendant overlap, with file
targets treated as collisions whenever a staging-managed directory contains
them. Extend the current exact-equality test at
`tests/test_staging.py:134-158` with both containment directions.

### Original finding disposition

1. **Remote PostgreSQL host — closed.** Both field-derived and URL-derived
   effective application targets must be loopback and must agree. Focused tests
   cover a remote `POSTGRES_HOST`, remote URL, and URL/field mismatch.
2. **Symlinked staging roots — closed for managed roots.** Validation walks each
   existing component lexically; creation uses directory file descriptors with
   `O_NOFOLLOW`; tests cover root/nested symlinks and a post-validation swap.
3. **Effective production targets — still open.** Exact application override
   collisions are detected, but blockers 1-3 above leave project, fixed Compose
   defaults, and path containment unprotected.
4. **WHOOP secret handling — closed.** Inline client ID/secret and
   `STAGING_METABASE_DB` are rejected; WHOOP auth/sync requires a regular
   non-symlink mode-`0600` credential file, while disconnected status remains
   usable. Non-synthetic env secrets require a mode-`0600` env file.
5. **Metabase DB override — closed.** `metabase_staging` is fixed in Compose/init,
   the override key is forbidden, and equality with the application DB fails.
6. **Actual root mode — closed.** Automated tests and both retained artifacts and
   this review's live smoke show `.staging` plus managed child directories at
   `0700`.

Gmail and Telegram roots, state, temporary data, OAuth/token files, vault, WHOOP
tokens, and credentials are all included in the managed staging environment and
are under `.staging`. The default subprocess drops managed production connector
variables before installing staging values. No credential values were printed or
found in tracked files.

### Independently reproduced automated gates

- `uv run pytest -q tests/test_staging.py`: **37 passed**; five existing PyMuPDF
  SWIG deprecation warnings.
- `uv run pytest -q`: **317 passed**; the same five warnings.
- `uv run ruff check .`: **passed**.
- `uv run mypy src`: **passed**, 41 source files.
- `docker compose --project-name health-agent-staging --env-file
  .env.staging.example --file compose.staging.yaml config --quiet`: **passed**.
- `uv lock --check`, `git diff --check 0988b67..HEAD`, and final
  `git diff --check`: **passed**.

Tests use a random disposable PostgreSQL container/database with explicit name
guards before cleanup. They do not use live credentials, WHOOP payloads, or
production health rows.

### Independently reproduced real smoke

Using the retained staging-only volumes, this review ran the documented sequence
and then stopped it again:

1. `staging start` brought up only project `health-agent-staging`; PostgreSQL
   became healthy and migration completed.
2. `staging run -- alembic current` returned `0005_whoop (head)`.
3. WHOOP status returned `configured=false`, `token=missing`, zero records, and
   no error without requiring credentials.
4. Dashboard setup returned `status=ready`, a staging URL on
   `http://127.0.0.1:54000`, and Metabase `/api/health` returned `{"status":"ok"}`.
5. Docker inspection showed only loopback bindings
   `127.0.0.1:56432 -> 5432` and `127.0.0.1:54000 -> 3000`, project label
   `health-agent-staging`, the staging init bind read-only, and only
   `health-agent-staging_staging_health_postgres` /
   `health-agent-staging_staging_health_metabase` volumes.
6. The separate production project `health-agent` remained running throughout.
   `staging stop` then left both staging containers stopped and both staging
   volumes retained. No `clean` or volume deletion was performed.
7. Actual modes for `.staging`, `vault`, `tmp`, `tokens`, `connector-state`, and
   `credentials` were all `0700`.

This independently confirms retained-volume restart, migration, application DB
routing, Metabase, command behavior, modes, labels, ports, mounts, and a clean
stop. The author's reported fresh-volume clean/recreate was not repeated because
that would delete retained state; it remains credible but not independently
proven by this re-review.

### Live-only concerns (not additional current blockers)

- No live WHOOP OAuth or payload was used. The required separate credential file,
  callback, token creation, full sync, and restart continuity still require the
  owner's explicit staging acceptance.
- Docker images are version-tagged rather than digest-pinned. This remains
  acceptable for the requested local, non-enterprise installation.
- The runner inherits the current Docker context/`DOCKER_HOST`. The reproduced
  smoke had no such environment override and used the local Docker Desktop. If
  remote Docker contexts are ever used on this Mac, local-only validation should
  reject them before destructive staging commands.

## Final fix re-review — 2026-09-04 (`a457eb1`)

- **SPEC: SHIP**
- **QUALITY: SHIP**
- **OVERALL: SHIP**

All remaining findings from the `926c720` review are closed.

- Production now has the explicit fixed Compose project `health-agent`, while
  staging remains `health-agent-staging`. Independent renders returned those two
  exact names. Effective production `COMPOSE_PROJECT_NAME` from `.env` or the
  process is also modeled and a staging-name collision fails before any Compose
  command (`compose.yaml:1`; `src/health_agent/staging.py:12,86-94,164-184,226-232`).
- Fixed production Compose targets are always retained: ports `55432`/`53000`,
  databases `health_agent`/`metabase`, and role `health_agent`. Effective
  production application overrides augment rather than replace them
  (`src/health_agent/staging.py:14-16,123-184`). Tests cover production overrides
  away from each fixed target followed by attempted staging reuse.
- Filesystem collision now uses canonical identities and rejects equality plus
  ancestor/descendant containment in both directions
  (`src/health_agent/staging.py:291-307,602-608`). Tests cover production
  `.staging`, exact `.staging/vault`, and `.staging/vault/production`.

Independent gates passed: **45** focused staging tests, **325** full-suite tests
(only the existing five PyMuPDF SWIG deprecation warnings), Ruff, mypy over 41
source files, `uv lock --check`, Compose validation/renders, and
`git diff --check 926c720..HEAD`.

The simultaneous retained-volume smoke also passed. While production project
`health-agent` remained healthy on loopback ports `55432`/`53000`, staging started
as project `health-agent-staging` on `56432`/`54000`, reached Alembic
`0005_whoop (head)`, returned disconnected WHOOP status with no credentials,
bootstrapped its dashboard, and returned Metabase health `ok`. Docker showed four
distinct containers and four project-specific volumes. All staging directories
were mode `0700`. `staging stop` stopped only staging; production remained running
and staging volumes were preserved.

No live WHOOP OAuth, credential, or payload was used. That owner-authorized
connector acceptance remains live-only and is not a blocker for the staging
isolation contour. No fresh-volume `clean` was repeated in this round.
