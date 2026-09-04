from __future__ import annotations

import json
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from health_agent.automation.models import AutomationJob, AutomationResult
from health_agent.automation.runner import AutomationRunner, SubprocessJobExecutor
from health_agent.automation.storage import AutomationState, GlobalRunLock
from health_agent.config import Settings

NOW = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


@dataclass
class FakeAdapter:
    source: str
    jobs: tuple[AutomationJob, ...] = ()
    error: Exception | None = None

    def discover(self, settings: Settings):
        del settings
        if self.error:
            raise self.error
        return self.jobs


class FakeLock:
    def __init__(self, acquired: bool = True) -> None:
        self.acquired = acquired
        self.released = False

    def acquire(self) -> bool:
        return self.acquired

    def release(self) -> None:
        self.released = True


class FakeExecutor:
    def __init__(self, outcomes: dict[tuple[str, str, str], str] | None = None) -> None:
        self.outcomes = outcomes or {}
        self.calls: list[tuple[tuple[str, str, str], str]] = []

    def execute(self, job: AutomationJob, mode: str) -> AutomationResult:
        self.calls.append((job.key, mode))
        status = self.outcomes.get(job.key, "succeeded")
        if status == "raise":
            raise RuntimeError("SECRET raw connector response")
        if status == "timed_out":
            return AutomationResult(*job.key, mode, "timed_out", "job_timeout")
        if status == "deferred":
            return AutomationResult(*job.key, mode, "deferred", "connector_deferred")
        return AutomationResult(*job.key, mode, status)  # type: ignore[arg-type]


def _job(source: str, profile: str, account: str) -> AutomationJob:
    return AutomationJob(source, profile, account, True, (source, "sync"))  # type: ignore[arg-type]


def _runner(tmp_path: Path, adapters, executor, lock, *, now=NOW) -> AutomationRunner:
    return AutomationRunner(
        Settings(),
        adapters,
        executor,
        AutomationState(tmp_path / "state.json"),
        lock,
        clock=lambda: now,
    )


def test_first_run_is_full_then_incremental_until_seven_days(tmp_path: Path) -> None:
    job = _job("whoop", "profile-1", "main")
    executor = FakeExecutor()
    lock = FakeLock()
    runner = _runner(tmp_path, [FakeAdapter("whoop", (job,))], executor, lock)
    assert runner.run()[0].status == "succeeded"
    assert executor.calls == [(job.key, "full")]
    state_path = tmp_path / "state.json"
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.parent.stat().st_mode) == 0o700

    second_executor = FakeExecutor()
    second = _runner(
        tmp_path,
        [FakeAdapter("whoop", (job,))],
        second_executor,
        FakeLock(),
        now=NOW + timedelta(days=6, hours=23),
    )
    second.run()
    assert second_executor.calls == [(job.key, "incremental")]

    due_executor = FakeExecutor()
    due = _runner(
        tmp_path,
        [FakeAdapter("whoop", (job,))],
        due_executor,
        FakeLock(),
        now=NOW + timedelta(days=7),
    )
    due.run()
    assert due_executor.calls == [(job.key, "full")]


def test_failed_deferred_and_timed_out_full_jobs_remain_due(tmp_path: Path) -> None:
    jobs = (
        _job("whoop", "profile-1", "main"),
        _job("gmail", "profile-1", "main"),
        _job("drive", "profile-1", "main"),
    )
    outcomes = {jobs[0].key: "raise", jobs[1].key: "deferred", jobs[2].key: "timed_out"}
    first_executor = FakeExecutor(outcomes)
    first = _runner(tmp_path, [FakeAdapter("whoop", jobs)], first_executor, FakeLock())
    results = first.run()
    assert [result.status for result in results] == ["timed_out", "deferred", "failed"]
    assert "SECRET" not in "\n".join(result.safe_line() for result in results)

    retry_executor = FakeExecutor()
    retry = _runner(tmp_path, [FakeAdapter("whoop", jobs)], retry_executor, FakeLock())
    retry.run()
    assert [mode for _, mode in retry_executor.calls] == ["full", "full", "full"]


def test_discovery_failure_isolated_and_empty_registry_succeeds(tmp_path: Path) -> None:
    job = _job("drive", "profile-2", "main")
    lock = FakeLock()
    results = _runner(
        tmp_path,
        [FakeAdapter("gmail", error=RuntimeError("private path")), FakeAdapter("drive", (job,))],
        FakeExecutor(),
        lock,
    ).run()
    assert [(result.source, result.status) for result in results] == [
        ("gmail", "failed"),
        ("drive", "succeeded"),
    ]
    assert lock.released
    assert _runner(tmp_path, [], FakeExecutor(), FakeLock()).run() == ()


def test_nonblocking_overlap_is_successful_skip(tmp_path: Path) -> None:
    result = _runner(tmp_path, [], FakeExecutor(), FakeLock(False)).run()
    assert result == (
        AutomationResult("runner", "none", "none", "none", "skipped", "already_running"),
    )


def test_real_global_lock_excludes_second_owner(tmp_path: Path) -> None:
    first = GlobalRunLock(tmp_path / "locks" / "sync.lock")
    second = GlobalRunLock(tmp_path / "locks" / "sync.lock")
    assert first.acquire()
    assert not second.acquire()
    first.release()
    assert second.acquire()
    second.release()


def test_subprocess_executor_discards_child_output_and_maps_fixed_statuses(
    monkeypatch, tmp_path: Path
) -> None:
    job = _job("whoop", "profile-1", "main")
    calls: list[dict] = []

    def fake_run(arguments, **kwargs):
        calls.append({"arguments": arguments, **kwargs})
        return subprocess.CompletedProcess(arguments, 0, "status=succeeded secret=LEAK\n", "LEAK")

    monkeypatch.setattr(subprocess, "run", fake_run)
    executor = SubprocessJobExecutor(Path("/bin/tool"), tmp_path / ".env", tmp_path)
    result = executor.execute(job, "full")
    assert result == AutomationResult("whoop", "profile-1", "main", "full", "succeeded")
    assert calls[0]["arguments"][-1] == "--full"
    assert calls[0]["shell"] is False
    assert calls[0]["capture_output"] is True
    assert calls[0]["timeout"] == 1800
    assert calls[0]["env"]["HEALTH_AGENT_ENV_FILE"] == str(tmp_path / ".env")
    assert "LEAK" not in result.safe_line()


def test_corrupt_or_symlinked_checkpoint_fails_without_running_job(tmp_path: Path) -> None:
    job = _job("whoop", "profile-1", "main")
    target = tmp_path / "target.json"
    target.write_text(json.dumps({"full_success": {}}), encoding="utf-8")
    (tmp_path / "state.json").symlink_to(target)
    executor = FakeExecutor()
    result = _runner(tmp_path, [FakeAdapter("whoop", (job,))], executor, FakeLock()).run()
    assert result[0].safe_error_code == "state_read_failed"
    assert executor.calls == []


def test_state_write_failure_is_reported_and_full_remains_due(tmp_path: Path) -> None:
    class FailingWriteState(AutomationState):
        def mark_full_success(self, job: AutomationJob, now: datetime) -> None:
            del job, now
            raise OSError("private filesystem detail")

    job = _job("whoop", "profile-1", "main")
    runner = AutomationRunner(
        Settings(),
        [FakeAdapter("whoop", (job,))],
        FakeExecutor(),
        FailingWriteState(tmp_path / "state.json"),
        FakeLock(),
        clock=lambda: NOW,
    )
    result = runner.run()[0]
    assert result.status == "failed"
    assert result.safe_error_code == "state_write_failed"
    assert not (tmp_path / "state.json").exists()
