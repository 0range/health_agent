# SDD ledger — always-on Telegram LaunchAgent

Base: `18d97a3`. Design: `4b0a3c6`. Plan: `8698ce2`.

- Task 1 RED: `health_agent.telegram.launchd` missing during collection.
- Task 1 GREEN: exact plist/lifecycle/rotation/runner tests passed; Ruff and mypy passed.
- Task 2 RED: all five lifecycle commands and `service-run` absent.
- Task 2 GREEN: CLI/runbook integrated; focused and full gates passed.
- Self-review hardened active-log reopening, hostile managed paths, strict modes,
  ambiguous launchctl status, and inherited singleton descriptor.
- Live launchd, Telegram, OpenAI and real credentials remain intentionally untouched.
- Independent review package is generated after this report commit; approval is not claimed.
- Lifecycle hardening RED reproduced ignored rollback-bootstrap failure and a
  concurrent install entering the winner's filesystem/launchctl transaction.
- Lifecycle hardening GREEN adds a fixed per-user lock shared across env roots,
  distinct bounded rollback failure metadata, and deterministic concurrency and
  double-bootstrap coverage. Final independent re-review remains required.
