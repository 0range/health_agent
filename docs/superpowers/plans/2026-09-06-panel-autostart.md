# Panel autostart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** Keep the local management/healthcheck page available after closing development terminals and logging into the Mac.
**Architecture:** Thin subclass of existing tested LaunchdManager overrides only service identity/plist payload; reuse lifecycle, rollback, private logs and path checks. No new scheduler or framework.
**Tech Stack:** Existing Python/plistlib/macOS launchd/Typer.

## Global Constraints

- Own only com.orange.health-agent.panel and exact panel-specific plist/log paths; never affect sync/Telegram/reminder jobs.
- Loopback-only existing panel serve; no credentials embedded in plist (only private env-file path).
- Readiness/loaded status must not claim HTTP health. Real GET tested separately at deployment.
- User approved autonomous completion and basicdailyuse polish; no new approval gate.

### Task 1: Reuse managed lifecycle for panel

Files create src/health_agent/panel/launchd.py and tests/panel/test_launchd.py; modify CLI panel subcommands only.
Interfaces `panel_launchd_paths(...) -> LaunchdPaths`, `PanelLaunchdManager(LaunchdManager)`; CLI panel install/status/stop --env-file. Paths built with existingresolve then dataclasses.replace exactpanelpaths, no changedbaseclass.

- [ ] Write failing tests assert exactprivateplist Label, panel serve args, HEALTH_AGENT_ENV_FILE path, KeepAlive/RunAtLoad true, ThrottleInterval30, panel-specificlogs; contentdoesnotcontainenvsecret.
```python
payload = plistlib.loads(manager.render().read_bytes())
assert payload['Label'] == 'com.orange.health-agent.panel'
assert payload['ProgramArguments'][1:] == ['panel', 'serve']
assert 'StartInterval' not in payload
```
- [ ] Implement subclasses/properties with no network and no process duringrender; managerinherits safeinstall/rollback/stop. CLI safeerror boundary contentfree; use current executable lookup sameasexistingautomation. No broadsourceformatting.
- [ ] Test fake-launchctl idempotentinstall, stopretainsfiles, lifecycleonlypanelservice, unsafeenv/pathrejection, CLI wiring/safeerrors. Run focusedpanel+existingautomationlaunchd tests Ruff/mypy, commit then independentreview before actualinstall.
- [ ] Deployment afterreview: identify current8766PIDbelongshealthagentpanel then gracefullystop; installpanelLaunchAgent, GET profile/healthcheck/medical200; restarttestviaownedserviceonly. Do not claimreboottestwithoutreboot.
