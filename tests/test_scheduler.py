from __future__ import annotations

import inspect
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from fanatic_agents.autonomous.models import (
    AutonomousRunReceipt,
    AutonomousRunResult,
    AutonomousTransition,
)
from fanatic_agents.autonomous.receipt import AutonomousRunReceiptStore
from fanatic_agents.core.config import ProjectConfig, SchedulerConfig
from fanatic_agents.intake.models import TaskIntakeReceipt
from fanatic_agents.intake.receipt import TaskIntakeReceiptStore
from fanatic_agents.observation.models import PullRequestObservation
from fanatic_agents.scheduler.service import SchedulerService
from fanatic_agents.scheduler.state import (
    SchedulerLockedError,
    SchedulerStateError,
    SchedulerStateStore,
)

NOW = datetime(2026, 8, 24, tzinfo=UTC)
SHA = "a" * 40


def _config(
    repository: Path, *, max_tasks: int = 2, max_errors: int = 3
) -> ProjectConfig:
    return ProjectConfig.model_validate({
        "project": {"name": "test"},
        "repository": {"path": str(repository), "main_branch": "main"},
        "commands": {"setup": [], "test": [], "build": []},
        "limits": {
            "max_tasks_per_day": max_tasks,
            "max_runtime_minutes": 10,
            "max_daily_cost_usd": 1.0,
            "max_iterations_per_task": 1,
        },
        "permissions": {
            "read_issues": True,
            "autonomous_execution": True,
            "observe_pull_request": True,
        },
        "intake": {"enabled": True},
        "autonomy": {"enabled": True},
        "scheduler": {
            "enabled": True,
            "interval_minutes": 15,
            "max_consecutive_errors": max_errors,
        },
    })


def _result(
    repository: Path,
    *,
    status: str = "no_eligible_tasks",
    issue: int | None = None,
    task_status: str | None = None,
) -> AutonomousRunResult:
    return AutonomousRunResult(
        repository=str(repository),
        github_repository="owner/repo",
        issue_number=issue,
        issue_url=(
            f"https://github.com/owner/repo/issues/{issue}" if issue else None
        ),
        task_id=f"github:owner/repo#{issue}" if issue else None,
        task_title=f"Task {issue}" if issue else None,
        task_status=task_status,
        started_at=NOW,
        finished_at=NOW,
        status=status,
    )


class FakeRunner:
    def __init__(self, results: list[AutonomousRunResult | Exception]) -> None:
        self.results = results
        self.calls = 0

    def run_once(self, *args: object, **kwargs: object) -> AutonomousRunResult:
        item = self.results[min(self.calls, len(self.results) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class FakeObservation:
    def __init__(self, statuses: list[str]) -> None:
        self.statuses = statuses
        self.calls = 0

    def observe_once(self, worktree: Path, **kwargs: object) -> PullRequestObservation:
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return PullRequestObservation(
            repository="owner/repo",
            promotion_worktree=str(worktree),
            pr_number=53,
            pr_url="https://github.com/owner/repo/pull/53",
            status=status,
            observed_at=NOW,
        )


class MutableClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def _service(
    repository: Path,
    metadata: Path,
    runner: FakeRunner,
    *,
    observation: FakeObservation | None = None,
    sleeper=None,
    clock=None,
) -> SchedulerService:
    return SchedulerService(
        runner=runner,
        observation=observation or FakeObservation(["waiting_for_ci"]),
        metadata_root=metadata,
        sleeper=sleeper,
        clock=clock or (lambda: NOW),
    )


def _active_receipts(
    repository: Path,
    metadata: Path,
    status: str,
    *,
    issue: int = 42,
) -> tuple[TaskIntakeReceiptStore, AutonomousRunReceiptStore]:
    tasks = TaskIntakeReceiptStore(metadata_root=metadata)
    runs = AutonomousRunReceiptStore(metadata_root=metadata)
    task = TaskIntakeReceipt(
        repository=str(repository),
        github_repository="owner/repo",
        issue_number=issue,
        issue_url=f"https://github.com/owner/repo/issues/{issue}",
        title="Task",
        selected_priority="p0",
        labels=["fanatic:ready"],
        base_branch="main",
        base_commit_sha=SHA,
        selected_at=NOW,
        task_status=status,
    )
    tasks.save(task)
    run = AutonomousRunReceipt(
        intake_receipt_path=str(tasks.path_for(repository, issue)),
        repository=str(repository),
        github_repository="owner/repo",
        task_id=f"github:owner/repo#{issue}",
        issue_number=issue,
        issue_url=f"https://github.com/owner/repo/issues/{issue}",
        task_title="Task",
        base_branch="main",
        base_commit_sha=SHA,
        task_status=status,
        transitions=[AutonomousTransition(state=status, at=NOW)],
        branch="fanatic/issue-42-task",
        worktree_path=str(metadata / "worktree"),
        pr_number=53,
        pr_url="https://github.com/owner/repo/pull/53",
        started_at=NOW,
        updated_at=NOW,
    )
    runs.save(run)
    return tasks, runs


def test_scheduler_config_is_strict_and_deny_by_default() -> None:
    assert SchedulerConfig() == SchedulerConfig(
        enabled=False, interval_minutes=15, max_consecutive_errors=3
    )
    for values in (
        {"interval_minutes": 0},
        {"interval_minutes": 1441},
        {"max_consecutive_errors": 0},
        {"max_consecutive_errors": 21},
        {"unexpected": True},
    ):
        with pytest.raises(ValidationError):
            SchedulerConfig.model_validate(values)


def test_disabled_scheduler_never_calls_runner(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([_result(repository)])
    config = _config(repository).model_copy(
        update={"scheduler": SchedulerConfig()}
    )
    result = _service(repository, tmp_path / "meta", runner).run_cycle(
        config, image="python:3.12-slim"
    )
    assert result.status == "scheduler_disabled"
    assert runner.calls == 0


def test_one_cycle_no_tasks_is_normal(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([_result(repository)])
    service = _service(repository, tmp_path / "meta", runner)
    result = service.run_cycle(_config(repository), image="image")
    assert result.status == "no_eligible_tasks"
    assert result.consecutive_errors == 0
    assert runner.calls == 1


def test_one_cycle_executes_exactly_one_task(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([
        _result(repository, status="verified", issue=1, task_status="verified")
    ])
    result = _service(repository, tmp_path / "meta", runner).run_cycle(
        _config(repository), image="image"
    )
    assert result.status == "task_started"
    assert result.task_claimed is True
    assert runner.calls == 1


def test_max_cycles_and_sleep_boundaries(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([_result(repository)])
    sleeps: list[float] = []
    service = _service(
        repository, tmp_path / "meta", runner, sleeper=sleeps.append
    )
    one = service.run_forever(
        _config(repository), image="image", max_cycles=1
    )
    assert one.cycles_executed == 1
    assert sleeps == []

    three = service.run_forever(
        _config(repository), image="image", max_cycles=3
    )
    assert three.cycles_executed == 3
    assert sleeps == [900, 900]
    assert runner.calls == 4


@pytest.mark.parametrize(
    "status",
    [
        "running", "verified", "promoted", "waiting_for_ci",
        "waiting_for_review", "ready_for_human_merge",
    ],
)
def test_active_task_blocks_new_selection(
    tmp_path: Path, status: str
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "meta"
    _active_receipts(repository, metadata, status)
    runner = FakeRunner([_result(repository)])
    observation = FakeObservation([status])
    result = _service(
        repository, metadata, runner, observation=observation
    ).run_cycle(_config(repository), image="image")
    assert runner.calls == 0
    assert result.issue_number == 42
    if status in {"running", "verified", "promoted"}:
        assert result.status == "active_task"
        assert observation.calls == 0
    else:
        assert observation.calls == 1


@pytest.mark.parametrize(
    ("observed", "expected"),
    [
        ("waiting_for_ci", "waiting_for_ci"),
        ("no_ci_reported", "waiting_for_ci"),
        ("waiting_for_review", "waiting_for_review"),
        ("ready_for_human_merge", "ready_for_human_merge"),
    ],
)
def test_pr_is_observed_exactly_once_per_cycle(
    tmp_path: Path, observed: str, expected: str
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "meta"
    _active_receipts(repository, metadata, "delivered")
    observation = FakeObservation([observed])
    runner = FakeRunner([_result(repository)])
    result = _service(
        repository, metadata, runner, observation=observation
    ).run_cycle(_config(repository), image="image")
    assert result.status == expected
    assert observation.calls == 1
    assert runner.calls == 0


def test_merged_cycle_stops_then_next_cycle_can_select(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "meta"
    tasks, runs = _active_receipts(
        repository, metadata, "waiting_for_ci"
    )
    observation = FakeObservation(["merged_externally"])
    runner = FakeRunner([_result(repository)])
    service = _service(
        repository, metadata, runner, observation=observation
    )
    first = service.run_cycle(_config(repository), image="image")
    assert first.status == "merged_externally"
    assert runner.calls == 0
    assert tasks.load(repository, 42).task_status == "merged_externally"
    assert runs.load(repository, 42).task_status == "merged_externally"

    second = service.run_cycle(_config(repository), image="image")
    assert second.status == "no_eligible_tasks"
    assert runner.calls == 1


@pytest.mark.parametrize("observed", ["ci_failed", "changes_requested"])
def test_ci_or_review_failure_requires_human_intervention(
    tmp_path: Path, observed: str
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "meta"
    tasks, _ = _active_receipts(repository, metadata, "delivered")
    result = _service(
        repository,
        metadata,
        FakeRunner([_result(repository)]),
        observation=FakeObservation([observed]),
    ).run_cycle(_config(repository), image="image")
    assert result.status == "manual_intervention_required"
    assert tasks.load(repository, 42).task_status == "failed"


def test_daily_limit_resets_on_next_utc_date(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    clock = MutableClock()
    runner = FakeRunner([
        _result(repository, status="verified", issue=1, task_status="failed"),
        _result(repository, status="verified", issue=2, task_status="failed"),
    ])
    service = _service(
        repository, tmp_path / "meta", runner, clock=clock
    )
    config = _config(repository, max_tasks=1)
    assert service.run_cycle(config, image="image").task_claimed
    assert service.run_cycle(config, image="image").status == (
        "daily_task_limit_reached"
    )
    assert runner.calls == 1
    clock.now += timedelta(days=1)
    assert service.run_cycle(config, image="image").task_claimed
    assert runner.calls == 2


def test_daily_counter_uses_utc_not_clock_offset(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    clock = MutableClock()
    clock.now = datetime(2026, 8, 24, 0, 30, tzinfo=UTC)
    runner = FakeRunner([
        _result(repository, status="verified", issue=1, task_status="failed"),
        _result(repository, status="verified", issue=2, task_status="failed"),
    ])
    service = _service(
        repository, tmp_path / "meta", runner, clock=clock
    )
    config = _config(repository, max_tasks=1)
    assert service.run_cycle(config, image="image").task_claimed
    clock.now = datetime(
        2026, 8, 23, 23, 30, tzinfo=timezone(timedelta(hours=-2))
    )
    assert service.run_cycle(config, image="image").status == (
        "daily_task_limit_reached"
    )
    assert runner.calls == 1


def test_transient_errors_wait_and_stop_at_configured_max(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([RuntimeError("unavailable")])
    sleeps: list[float] = []
    result = _service(
        repository, tmp_path / "meta", runner, sleeper=sleeps.append
    ).run_forever(
        _config(repository, max_errors=2), image="image", max_cycles=5
    )
    assert result.status == "too_many_consecutive_errors"
    assert result.cycles_executed == 2
    assert sleeps == [900]
    assert runner.calls == 2


def test_github_unavailable_is_structured_and_counted(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([
        _result(repository, status="github_unavailable")
    ])
    result = _service(
        repository, tmp_path / "meta", runner
    ).run_cycle(_config(repository), image="image")
    assert result.status == "github_unavailable"
    assert result.consecutive_errors == 1


def test_success_resets_consecutive_errors(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    runner = FakeRunner([RuntimeError("down"), _result(repository)])
    service = _service(repository, tmp_path / "meta", runner)
    assert service.run_cycle(
        _config(repository), image="image"
    ).consecutive_errors == 1
    assert service.run_cycle(
        _config(repository), image="image"
    ).consecutive_errors == 0


def test_keyboard_interrupt_releases_lock_and_persists_stop(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "meta"

    def interrupted(_seconds: float) -> None:
        raise KeyboardInterrupt

    service = _service(
        repository, metadata, FakeRunner([_result(repository)]),
        sleeper=interrupted,
    )
    result = service.run_forever(_config(repository), image="image")
    store = SchedulerStateStore(metadata_root=metadata)
    assert result.status == "stopped_by_user"
    assert "Scheduler stopped by user." in (result.stop_reason or "")
    assert not store.lock_path(repository).exists()
    assert store.load(repository).last_result_status == "stopped_by_user"


def test_scheduler_lock_recovery_is_conservative(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SchedulerStateStore(metadata_root=tmp_path / "meta")
    lock = store.lock_path(repository)
    with store.lock(repository):
        with pytest.raises(SchedulerLockedError):
            with store.lock(repository):
                pass
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("corrupt", encoding="utf-8")
    with pytest.raises(SchedulerLockedError):
        with store.lock(repository):
            pass
    assert lock.read_text(encoding="utf-8") == "corrupt"


def test_old_dead_scheduler_lock_is_recovered(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SchedulerStateStore(metadata_root=tmp_path / "meta")
    lock = store.lock_path(repository)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({
        "pid": 99999999,
        "timestamp": (NOW - timedelta(hours=2)).isoformat(),
        "repository": str(repository.resolve()),
    }), encoding="utf-8")
    with store.lock(repository):
        assert lock.exists()
    assert not lock.exists()


def test_corrupt_state_is_preserved_and_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SchedulerStateStore(metadata_root=tmp_path / "meta")
    path = store.state_path(repository)
    path.parent.mkdir(parents=True)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(SchedulerStateError):
        store.load_or_create(repository)
    assert path.read_text(encoding="utf-8") == "{bad"


def test_scheduler_source_has_no_direct_mutation_commands() -> None:
    source = inspect.getsource(SchedulerService)
    for forbidden in (
        "git checkout", "git switch", "git reset", "git merge",
        "git push", "gh pr merge", "gh issue create", "gh issue edit",
        "gh issue close", "gh issue comment",
    ):
        assert forbidden not in source


def test_recent_dead_looking_lock_is_preserved(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SchedulerStateStore(metadata_root=tmp_path / "meta")
    lock = store.lock_path(repository)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(json.dumps({
        "pid": 99999999,
        "timestamp": datetime.now(UTC).isoformat(),
        "repository": str(repository.resolve()),
    }), encoding="utf-8")
    with pytest.raises(SchedulerLockedError):
        with store.lock(repository):
            pass
    assert lock.exists()


def test_state_and_lock_use_private_permissions(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    store = SchedulerStateStore(metadata_root=tmp_path / "meta")
    state = store.load_or_create(repository, now=NOW)
    assert (store.state_path(repository).stat().st_mode & 0o777) == 0o600
    with store.lock(repository):
        assert (store.lock_path(repository).stat().st_mode & 0o777) == 0o600
    assert state.repository == str(repository.resolve())


def test_scheduler_cli_help_exposes_required_boundary() -> None:
    from typer.testing import CliRunner

    from fanatic_agents.cli.main import app

    runner = CliRunner()
    group = runner.invoke(app, ["scheduler", "--help"])
    command = runner.invoke(app, ["scheduler", "run", "--help"])
    assert group.exit_code == 0
    assert command.exit_code == 0
    assert "--config" in command.stdout
    assert "--image" in command.stdout
    assert "--deliver" in command.stdout
    assert "--max-cycles" in command.stdout
