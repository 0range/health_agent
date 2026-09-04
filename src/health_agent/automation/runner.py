"""Failure-isolated sequential execution for connector jobs."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from health_agent.automation.models import (
    AutomationJob,
    AutomationMode,
    AutomationResult,
)
from health_agent.automation.registry import JobAdapter
from health_agent.automation.storage import AutomationState, GlobalRunLock
from health_agent.config import Settings

JOB_TIMEOUT_SECONDS = 1800


class JobExecutor(Protocol):
    def execute(self, job: AutomationJob, mode: AutomationMode) -> AutomationResult: ...


class SubprocessJobExecutor:
    def __init__(self, executable: Path, env_file: Path, working_directory: Path) -> None:
        self.executable = executable
        self.env_file = env_file
        self.working_directory = working_directory

    def execute(self, job: AutomationJob, mode: AutomationMode) -> AutomationResult:
        arguments = [str(self.executable), *job.arguments]
        if mode == "full":
            arguments.append("--full")
        environment = os.environ.copy()
        environment["HEALTH_AGENT_ENV_FILE"] = str(self.env_file)
        try:
            completed = subprocess.run(
                arguments,
                cwd=self.working_directory,
                env=environment,
                shell=False,
                capture_output=True,
                text=True,
                timeout=JOB_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return AutomationResult(*job.key, mode, "timed_out", "job_timeout")
        except OSError:
            return AutomationResult(*job.key, mode, "failed", "spawn_failed")
        if completed.returncode != 0:
            return AutomationResult(*job.key, mode, "failed", "connector_failed")
        statuses = {
            field.split("=", 1)[1]
            for line in completed.stdout.splitlines()
            for field in line.split()
            if field.startswith("status=") and "=" in field
        }
        if statuses and statuses <= {"succeeded", "synced"}:
            return AutomationResult(*job.key, mode, "succeeded")
        if statuses == {"deferred"}:
            return AutomationResult(*job.key, mode, "deferred", "connector_deferred")
        return AutomationResult(*job.key, mode, "failed", "unknown_status")


class AutomationRunner:
    def __init__(
        self,
        settings: Settings,
        adapters: Sequence[JobAdapter],
        executor: JobExecutor,
        state: AutomationState,
        lock: GlobalRunLock,
        *,
        clock: Callable[[], datetime] | None = None,
        before_jobs: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.adapters = adapters
        self.executor = executor
        self.state = state
        self.lock = lock
        self.clock = clock or (lambda: datetime.now(UTC))
        self.before_jobs = before_jobs

    def run(self, force_full: bool = False) -> tuple[AutomationResult, ...]:
        try:
            acquired = self.lock.acquire()
        except Exception:  # noqa: BLE001 - emit only a fixed, content-free code
            return (AutomationResult("runner", "none", "none", "none", "failed", "lock_unavailable"),)
        if not acquired:
            return (AutomationResult("runner", "none", "none", "none", "skipped", "already_running"),)
        try:
            if self.before_jobs is not None:
                try:
                    self.before_jobs()
                except Exception:  # noqa: BLE001 - local log details stay private
                    return (
                        AutomationResult(
                            "runner",
                            "none",
                            "none",
                            "none",
                            "failed",
                            "log_rotation_failed",
                        ),
                    )
            jobs, discovery_results = self._discover()
            results = list(discovery_results)
            for job in jobs:
                now = self.clock()
                try:
                    full = job.supports_full and (
                        force_full or self.state.full_due(job, now)
                    )
                except Exception:  # noqa: BLE001 - local state details are private
                    results.append(AutomationResult(*job.key, "none", "failed", "state_read_failed"))
                    continue
                mode: AutomationMode = "full" if full else "incremental"
                try:
                    result = self.executor.execute(job, mode)
                except Exception:  # noqa: BLE001 - injected/connector details stay private
                    result = AutomationResult(*job.key, mode, "failed", "executor_failed")
                if mode == "full" and result.status == "succeeded":
                    try:
                        self.state.mark_full_success(job, now)
                    except Exception:  # noqa: BLE001 - do not claim an undurable full
                        result = AutomationResult(*job.key, mode, "failed", "state_write_failed")
                results.append(result)
            return tuple(results)
        finally:
            self.lock.release()

    def _discover(self) -> tuple[tuple[AutomationJob, ...], tuple[AutomationResult, ...]]:
        jobs: list[AutomationJob] = []
        failures: list[AutomationResult] = []
        for adapter in self.adapters:
            try:
                jobs.extend(adapter.discover(self.settings))
            except Exception:  # noqa: BLE001 - configuration details remain private
                failures.append(
                    AutomationResult(
                        adapter.source, "none", "none", "none", "failed", "discovery_failed"
                    )
                )
        return tuple(sorted(jobs, key=lambda job: job.key)), tuple(failures)
