"""Strict, secret-free scheduler contracts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.autonomous.models import AutonomousTaskStatus
from fanatic_agents.core.project import NonEmptyStrictString, StrictModel

SchedulerCycleStatus = Literal[
    "scheduler_disabled",
    "permission_denied",
    "no_eligible_tasks",
    "task_started",
    "active_task",
    "waiting_for_ci",
    "waiting_for_review",
    "ready_for_human_merge",
    "merged_externally",
    "manual_intervention_required",
    "daily_task_limit_reached",
    "github_unavailable",
    "scheduler_error",
    "too_many_consecutive_errors",
]
SchedulerRunStatus = Literal[
    "max_cycles_reached",
    "stopped_by_user",
    "too_many_consecutive_errors",
    "scheduler_disabled",
    "permission_denied",
    "scheduler_failed",
]


class SchedulerState(StrictModel):
    """External scheduler bookkeeping for one repository."""

    schema_version: Literal[1] = 1
    repository: NonEmptyStrictString
    started_at: datetime
    updated_at: datetime
    cycles_completed: int = Field(default=0, strict=True, ge=0)
    tasks_started_today: int = Field(default=0, strict=True, ge=0)
    counter_date: date
    consecutive_errors: int = Field(default=0, strict=True, ge=0)
    last_cycle_at: datetime | None = None
    last_result_status: NonEmptyStrictString | None = None
    active_issue_number: int | None = Field(default=None, strict=True, gt=0)
    active_task_id: NonEmptyStrictString | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "SchedulerState":
        timestamps = [self.started_at, self.updated_at]
        if self.last_cycle_at is not None:
            timestamps.append(self.last_cycle_at)
        if any(value.utcoffset() is None for value in timestamps):
            raise ValueError("scheduler timestamps must include a timezone")
        if self.updated_at < self.started_at:
            raise ValueError("scheduler updated_at cannot precede started_at")
        if (self.active_issue_number is None) != (self.active_task_id is None):
            raise ValueError("active scheduler task identity must be complete")
        return self


class SchedulerCycleResult(StrictModel):
    """Outcome of one bounded cycle with no internal task loop."""

    repository: NonEmptyStrictString
    status: SchedulerCycleStatus
    started_at: datetime
    finished_at: datetime
    issue_number: int | None = Field(default=None, strict=True, gt=0)
    task_id: str | None = None
    task_status: AutonomousTaskStatus | None = None
    autonomous_status: str | None = None
    observation_status: str | None = None
    task_claimed: bool = False
    consecutive_errors: int = Field(default=0, strict=True, ge=0)
    stop_reason: str | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> "SchedulerCycleResult":
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("cycle timestamps must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("cycle finished_at cannot precede started_at")
        return self

    @property
    def final_status(self) -> str:
        return self.status.upper()


class SchedulerRunResult(StrictModel):
    """Terminal result for one foreground scheduler invocation."""

    repository: NonEmptyStrictString
    status: SchedulerRunStatus
    cycles_executed: int = Field(default=0, strict=True, ge=0)
    cycles_completed: int = Field(default=0, strict=True, ge=0)
    tasks_started_today: int = Field(default=0, strict=True, ge=0)
    consecutive_errors: int = Field(default=0, strict=True, ge=0)
    last_cycle_status: str | None = None
    stop_reason: str | None = None

    @property
    def final_status(self) -> str:
        return self.status.upper()
