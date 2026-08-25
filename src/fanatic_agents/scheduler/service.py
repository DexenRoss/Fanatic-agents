"""Foreground scheduler composition with bounded, single-task cycles."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fanatic_agents.autonomous.models import AutonomousRunReceipt, AutonomousRunResult
from fanatic_agents.autonomous.receipt import AutonomousRunReceiptStore
from fanatic_agents.autonomous.service import AutonomousRunner
from fanatic_agents.core.config import ProjectConfig
from fanatic_agents.intake.models import TaskIntakeReceipt
from fanatic_agents.intake.receipt import (
    ACTIVE_TASK_STATUSES,
    TaskIntakeReceiptStore,
)
from fanatic_agents.observation.models import PullRequestObservation
from fanatic_agents.observation.service import PullRequestObservationService
from fanatic_agents.scheduler.models import (
    SchedulerCycleResult,
    SchedulerRunResult,
    SchedulerState,
)
from fanatic_agents.scheduler.state import (
    SchedulerLockedError,
    SchedulerStateError,
    SchedulerStateStore,
)

RECONCILABLE_STATUSES = {
    "delivered", "waiting_for_ci", "waiting_for_review", "ready_for_human_merge"
}
PENDING_OBSERVATIONS = {
    "waiting_for_ci": "waiting_for_ci",
    "no_ci_reported": "waiting_for_ci",
    "waiting_for_review": "waiting_for_review",
    "ready_for_human_merge": "ready_for_human_merge",
}
TRANSIENT_OBSERVATIONS = {"github_unavailable", "observation_failed"}
MANUAL_OBSERVATIONS = {
    "ci_failed", "changes_requested", "merge_conflict", "pr_draft",
    "pr_closed", "pr_head_drifted", "invalid_delivery",
}
ACTIVE_AUTONOMOUS_STATUSES = {
    "running", "verified", "promoted", *RECONCILABLE_STATUSES
}
STATUS_RANK = {
    "delivered": 0, "waiting_for_ci": 1,
    "waiting_for_review": 2, "ready_for_human_merge": 3,
}


class AutonomousCycleRunner(Protocol):
    def run_once(
        self,
        project_config: ProjectConfig,
        *,
        image: str,
        repository: Path | None = None,
        deliver: bool = False,
    ) -> AutonomousRunResult: ...


class ObservationCycleRunner(Protocol):
    def observe_once(
        self, worktree: Path, **kwargs: object
    ) -> PullRequestObservation: ...


class SchedulerBoundaryError(RuntimeError):
    """A transient or ambiguous cycle boundary failed closed."""

    def __init__(self, message: str, *, status: str = "scheduler_error") -> None:
        super().__init__(message)
        self.status = status


class SchedulerService:
    """Run serial foreground cycles without task, observation, or retry loops."""

    def __init__(
        self,
        *,
        runner: AutonomousCycleRunner | None = None,
        observation: ObservationCycleRunner | None = None,
        task_receipts: TaskIntakeReceiptStore | None = None,
        run_receipts: AutonomousRunReceiptStore | None = None,
        states: SchedulerStateStore | None = None,
        metadata_root: Path | None = None,
        clock: Callable[[], datetime] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self._task_receipts = task_receipts or TaskIntakeReceiptStore(
            metadata_root=metadata_root
        )
        self._run_receipts = run_receipts or AutonomousRunReceiptStore(
            metadata_root=metadata_root
        )
        self._states = states or SchedulerStateStore(metadata_root=metadata_root)
        self._runner = runner or AutonomousRunner(
            task_receipts=self._task_receipts,
            run_receipts=self._run_receipts,
        )
        self._observation = observation or PullRequestObservationService()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleeper = sleeper or time.sleep

    def run_cycle(
        self,
        project_config: ProjectConfig,
        *,
        image: str,
        repository: Path | None = None,
        deliver: bool = False,
    ) -> SchedulerCycleResult:
        """Run exactly one scheduler decision and persist its result."""
        started = self._clock()
        requested = Path(
            repository if repository is not None else project_config.repository.path
        ).expanduser()
        repository_text = str(requested.resolve(strict=False))
        denied = _authorization_failure(project_config)
        if denied is not None:
            return SchedulerCycleResult(
                repository=repository_text,
                status=denied[0],
                started_at=started,
                finished_at=self._clock(),
                stop_reason=denied[1],
            )

        resolved = requested.resolve(strict=True)
        state = _reset_daily_counter(
            self._states.load_or_create(resolved, now=started), started
        )
        try:
            result, active = self._run_cycle_body(
                project_config, resolved, image=image,
                deliver=deliver, started=started,
            )
        except KeyboardInterrupt:
            raise
        except SchedulerBoundaryError as exc:
            return self._record_error(
                state, project_config, started, str(exc),
                error_status=exc.status,
            )
        except Exception:
            return self._record_error(
                state, project_config, started,
                "A scheduler cycle boundary failed safely; "
                "no immediate retry was made.",
            )

        updated = _next_state(
            state,
            now=self._clock(),
            status=result.status,
            active=active,
            claimed=result.task_claimed,
            consecutive_errors=0,
        )
        self._states.save(updated)
        return result.model_copy(update={
            "finished_at": updated.updated_at,
            "consecutive_errors": 0,
        })

    def run_forever(
        self,
        project_config: ProjectConfig,
        *,
        image: str,
        repository: Path | None = None,
        deliver: bool = False,
        max_cycles: int | None = None,
    ) -> SchedulerRunResult:
        """Hold the scheduler lock and run serial foreground cycles."""
        if (
            max_cycles is not None
            and (
                not isinstance(max_cycles, int)
                or isinstance(max_cycles, bool)
                or max_cycles < 1
            )
        ):
            raise ValueError("max_cycles must be greater than or equal to one")

        requested = Path(
            repository if repository is not None else project_config.repository.path
        ).expanduser()
        repository_text = str(requested.resolve(strict=False))
        denied = _authorization_failure(project_config)
        if denied is not None:
            return SchedulerRunResult(
                repository=repository_text,
                status=denied[0],
                stop_reason=denied[1],
            )

        resolved = requested.resolve(strict=True)
        cycles_executed = 0
        last_cycle: SchedulerCycleResult | None = None
        try:
            with self._states.lock(resolved):
                try:
                    while True:
                        last_cycle = self.run_cycle(
                            project_config, image=image,
                            repository=resolved, deliver=deliver,
                        )
                        cycles_executed += 1
                        if last_cycle.status == "too_many_consecutive_errors":
                            return self._run_result(
                                resolved, "too_many_consecutive_errors",
                                cycles_executed, last_cycle,
                                "Maximum consecutive scheduler errors reached.",
                            )
                        if max_cycles is not None and cycles_executed >= max_cycles:
                            return self._run_result(
                                resolved, "max_cycles_reached",
                                cycles_executed, last_cycle, None,
                            )
                        self._sleeper(
                            project_config.scheduler.interval_minutes * 60
                        )
                except KeyboardInterrupt:
                    self._persist_user_stop(resolved)
                    return self._run_result(
                        resolved, "stopped_by_user", cycles_executed,
                        last_cycle, "Scheduler stopped by user.",
                    )
        except SchedulerLockedError as exc:
            return SchedulerRunResult(
                repository=repository_text,
                status="scheduler_failed",
                stop_reason=str(exc),
            )
        except (SchedulerStateError, OSError, ValueError) as exc:
            return SchedulerRunResult(
                repository=repository_text,
                status="scheduler_failed",
                stop_reason=str(exc),
            )

    def _run_cycle_body(
        self,
        config: ProjectConfig,
        repository: Path,
        *,
        image: str,
        deliver: bool,
        started: datetime,
    ) -> tuple[SchedulerCycleResult, tuple[int, str] | None]:
        active_run, active_task = self._find_active(repository)
        if active_run is not None:
            identity = (active_run.issue_number, active_run.task_id)
            if active_run.task_status in RECONCILABLE_STATUSES:
                return self._reconcile(
                    config, repository, active_run, active_task, started
                )
            return (
                _cycle_result(
                    repository, "active_task", started,
                    issue_number=active_run.issue_number,
                    task_id=active_run.task_id,
                    task_status=active_run.task_status,
                    reason=(
                        "An existing task remains active and requires "
                        "reconciliation or human action before new work can start."
                    ),
                ),
                identity,
            )
        if active_task is not None:
            task_id = (
                f"github:{active_task.github_repository}"
                f"#{active_task.issue_number}"
            )
            return (
                _cycle_result(
                    repository, "active_task", started,
                    issue_number=active_task.issue_number,
                    task_id=task_id,
                    reason=(
                        "An intake receipt is active without safely resumable "
                        "autonomous state; new work is blocked."
                    ),
                ),
                (active_task.issue_number, task_id),
            )

        state = _reset_daily_counter(
            self._states.load_or_create(repository, now=started), started
        )
        if state.tasks_started_today >= config.limits.max_tasks_per_day:
            return (
                _cycle_result(
                    repository, "daily_task_limit_reached", started,
                    reason="The configured UTC daily task limit has been reached.",
                ),
                None,
            )

        autonomous = self._runner.run_once(
            config, image=image, repository=repository, deliver=deliver
        )
        claimed = (
            autonomous.issue_number is not None
            and autonomous.task_status is not None
        )
        common = {
            "issue_number": autonomous.issue_number,
            "task_id": autonomous.task_id,
            "task_status": autonomous.task_status,
            "autonomous_status": autonomous.status,
            "task_claimed": claimed,
            "reason": autonomous.stop_reason,
        }
        if autonomous.status == "no_eligible_tasks":
            return _cycle_result(
                repository, "no_eligible_tasks", started, **common
            ), None
        if autonomous.status == "github_unavailable":
            raise SchedulerBoundaryError(
                autonomous.stop_reason or "GitHub was unavailable.",
                status="github_unavailable",
            )
        if autonomous.status == "autonomous_run_failed":
            raise SchedulerBoundaryError(
                autonomous.stop_reason or "Autonomous execution was unavailable."
            )
        if autonomous.task_status == "failed":
            return _cycle_result(
                repository, "manual_intervention_required", started, **common
            ), None
        if autonomous.task_status == "merged_externally":
            return _cycle_result(
                repository, "merged_externally", started, **common
            ), None
        if autonomous.task_status in ACTIVE_AUTONOMOUS_STATUSES:
            assert autonomous.issue_number is not None
            task_id = autonomous.task_id or (
                f"github:{autonomous.github_repository}"
                f"#{autonomous.issue_number}"
            )
            return _cycle_result(
                repository, "task_started", started, **common
            ), (autonomous.issue_number, task_id)
        if claimed:
            return _cycle_result(
                repository, "manual_intervention_required", started, **common
            ), None
        raise SchedulerBoundaryError(
            autonomous.stop_reason or "Autonomous execution stopped unexpectedly."
        )

    def _find_active(
        self, repository: Path
    ) -> tuple[AutonomousRunReceipt | None, TaskIntakeReceipt | None]:
        runs = [
            item
            for item in self._run_receipts.list_for_repository(repository)
            if item.task_status in ACTIVE_AUTONOMOUS_STATUSES
        ]
        tasks = [
            item
            for item in self._task_receipts.list_for_repository(repository)
            if item.task_status in ACTIVE_TASK_STATUSES
        ]
        identities = (
            {item.issue_number for item in runs}
            | {item.issue_number for item in tasks}
        )
        if len(identities) > 1 or len(runs) > 1:
            raise SchedulerBoundaryError(
                "Multiple active task receipts were found; "
                "scheduler stopped closed."
            )
        active_run = runs[0] if runs else None
        active_task = tasks[0] if tasks else None
        if (
            active_run is not None
            and active_task is not None
            and (
                active_run.issue_number != active_task.issue_number
                or active_run.github_repository.casefold()
                != active_task.github_repository.casefold()
            )
        ):
            raise SchedulerBoundaryError(
                "Active task receipt identities do not match."
            )
        return active_run, active_task

    def _reconcile(
        self,
        config: ProjectConfig,
        repository: Path,
        run: AutonomousRunReceipt,
        task: TaskIntakeReceipt | None,
        started: datetime,
    ) -> tuple[SchedulerCycleResult, tuple[int, str] | None]:
        if run.worktree_path is None:
            self._transition_terminal(run, task, "failed", "invalid_delivery")
            return (
                _cycle_result(
                    repository, "manual_intervention_required", started,
                    issue_number=run.issue_number,
                    task_id=run.task_id,
                    task_status="failed",
                    observation_status="invalid_delivery",
                    reason="Active delivery state has no promotion worktree.",
                ),
                None,
            )

        observation = self._observation.observe_once(
            Path(run.worktree_path),
            permissions=config.permissions,
            configured_repository=Path(config.repository.path),
        )
        if observation.status in TRANSIENT_OBSERVATIONS:
            raise SchedulerBoundaryError(
                observation.stop_reason
                or "Pull request observation was unavailable.",
                status=(
                    "github_unavailable"
                    if observation.status == "github_unavailable"
                    else "scheduler_error"
                ),
            )
        if observation.status == "merged_externally":
            self._transition_terminal(
                run, task, "merged_externally", observation.status
            )
            return (
                _cycle_result(
                    repository, "merged_externally", started,
                    issue_number=run.issue_number,
                    task_id=run.task_id,
                    task_status="merged_externally",
                    observation_status=observation.status,
                    reason=observation.stop_reason,
                ),
                None,
            )
        if observation.status in MANUAL_OBSERVATIONS:
            self._transition_terminal(
                run, task, "failed", observation.status
            )
            return (
                _cycle_result(
                    repository, "manual_intervention_required", started,
                    issue_number=run.issue_number,
                    task_id=run.task_id,
                    task_status="failed",
                    observation_status=observation.status,
                    reason=observation.stop_reason,
                ),
                None,
            )

        target = PENDING_OBSERVATIONS[observation.status]
        self._record_pending_observation(
            run, task, target, observation.status
        )
        status = (
            "waiting_for_ci"
            if observation.status in {"waiting_for_ci", "no_ci_reported"}
            else observation.status
        )
        return (
            _cycle_result(
                repository, status, started,
                issue_number=run.issue_number,
                task_id=run.task_id,
                task_status=target,
                observation_status=observation.status,
                reason=observation.stop_reason,
            ),
            (run.issue_number, run.task_id),
        )

    def _record_pending_observation(
        self,
        run: AutonomousRunReceipt,
        task: TaskIntakeReceipt | None,
        target: str,
        observation_status: str,
    ) -> None:
        current_rank = STATUS_RANK.get(run.task_status, -1)
        target_rank = STATUS_RANK[target]
        if target_rank > current_rank:
            self._run_receipts.transition(
                run, target, observation_status=observation_status
            )
            if task is not None and task.task_status != target:
                self._task_receipts.transition(task, target)
        else:
            self._run_receipts.update(
                run, observation_status=observation_status
            )

    def _transition_terminal(
        self,
        run: AutonomousRunReceipt,
        task: TaskIntakeReceipt | None,
        target: str,
        observation_status: str,
    ) -> None:
        self._run_receipts.transition(
            run, target, observation_status=observation_status
        )
        if task is not None and task.task_status != target:
            self._task_receipts.transition(task, target)

    def _record_error(
        self,
        state: SchedulerState,
        config: ProjectConfig,
        started: datetime,
        reason: str,
        *,
        error_status: str = "scheduler_error",
    ) -> SchedulerCycleResult:
        errors = state.consecutive_errors + 1
        status = (
            "too_many_consecutive_errors"
            if errors >= config.scheduler.max_consecutive_errors
            else error_status
        )
        now = self._clock()
        updated = _next_state(
            state, now=now, status=status,
            active=_active_from_state(state), claimed=False,
            consecutive_errors=errors,
        )
        self._states.save(updated)
        return SchedulerCycleResult(
            repository=state.repository,
            status=status,
            started_at=started,
            finished_at=now,
            issue_number=state.active_issue_number,
            task_id=state.active_task_id,
            consecutive_errors=errors,
            stop_reason=reason,
        )

    def _persist_user_stop(self, repository: Path) -> None:
        now = self._clock()
        state = self._states.load_or_create(repository, now=now)
        self._states.save(state.model_copy(update={
            "updated_at": now,
            "last_result_status": "stopped_by_user",
        }))

    def _run_result(
        self,
        repository: Path,
        status: str,
        cycles_executed: int,
        last_cycle: SchedulerCycleResult | None,
        reason: str | None,
    ) -> SchedulerRunResult:
        state = self._states.load_or_create(repository, now=self._clock())
        return SchedulerRunResult(
            repository=str(repository),
            status=status,
            cycles_executed=cycles_executed,
            cycles_completed=state.cycles_completed,
            tasks_started_today=state.tasks_started_today,
            consecutive_errors=state.consecutive_errors,
            last_cycle_status=(
                last_cycle.status if last_cycle is not None else None
            ),
            stop_reason=reason,
        )


def _authorization_failure(
    config: ProjectConfig,
) -> tuple[str, str] | None:
    if not config.scheduler.enabled:
        return "scheduler_disabled", "Project scheduler is disabled."
    if not config.autonomy.enabled:
        return (
            "permission_denied",
            "Project autonomy must be explicitly enabled.",
        )
    if not config.intake.enabled:
        return (
            "permission_denied",
            "Task intake must be explicitly enabled.",
        )
    if not config.permissions.read_issues:
        return (
            "permission_denied",
            "Project configuration denies read_issues.",
        )
    if not config.permissions.autonomous_execution:
        return (
            "permission_denied",
            "Project configuration denies autonomous_execution.",
        )
    return None


def _reset_daily_counter(
    state: SchedulerState, now: datetime
) -> SchedulerState:
    utc_date = now.astimezone(UTC).date()
    if state.counter_date == utc_date:
        return state
    return state.model_copy(update={
        "counter_date": utc_date,
        "tasks_started_today": 0,
        "updated_at": now,
    })


def _next_state(
    state: SchedulerState,
    *,
    now: datetime,
    status: str,
    active: tuple[int, str] | None,
    claimed: bool,
    consecutive_errors: int,
) -> SchedulerState:
    return state.model_copy(update={
        "updated_at": now,
        "cycles_completed": state.cycles_completed + 1,
        "tasks_started_today": state.tasks_started_today + int(claimed),
        "consecutive_errors": consecutive_errors,
        "last_cycle_at": now,
        "last_result_status": status,
        "active_issue_number": (
            active[0] if active is not None else None
        ),
        "active_task_id": active[1] if active is not None else None,
    })


def _active_from_state(
    state: SchedulerState,
) -> tuple[int, str] | None:
    if (
        state.active_issue_number is None
        or state.active_task_id is None
    ):
        return None
    return state.active_issue_number, state.active_task_id


def _cycle_result(
    repository: Path,
    status: str,
    started: datetime,
    *,
    issue_number: int | None = None,
    task_id: str | None = None,
    task_status: str | None = None,
    autonomous_status: str | None = None,
    observation_status: str | None = None,
    task_claimed: bool = False,
    reason: str | None = None,
) -> SchedulerCycleResult:
    return SchedulerCycleResult(
        repository=str(repository),
        status=status,
        started_at=started,
        finished_at=started,
        issue_number=issue_number,
        task_id=task_id,
        task_status=task_status,
        autonomous_status=autonomous_status,
        observation_status=observation_status,
        task_claimed=task_claimed,
        stop_reason=reason,
    )

