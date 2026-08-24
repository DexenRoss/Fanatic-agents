"""Strict, secret-free contracts for one-shot autonomous execution."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel
from fanatic_agents.intake.models import Priority

AutonomousStatus = Literal[
    "no_eligible_tasks",
    "task_revoked",
    "repository_dirty",
    "base_repository_drifted",
    "workflow_rejected",
    "implementation_failed",
    "verification_failed",
    "verified",
    "promotion_failed",
    "promoted",
    "delivery_failed",
    "delivered_for_review",
    "waiting_for_ci",
    "waiting_for_review",
    "ready_for_human_merge",
    "branch_already_exists",
    "github_unavailable",
    "autonomy_disabled",
    "permission_denied",
    "autonomous_run_failed",
]
AutonomousTaskStatus = Literal[
    "selected",
    "running",
    "verified",
    "promoted",
    "delivered",
    "waiting_for_ci",
    "waiting_for_review",
    "ready_for_human_merge",
    "failed",
    "merged_externally",
]


class AutonomousTransition(StrictModel):
    """One persisted lifecycle transition without prompts or repository content."""

    state: AutonomousTaskStatus
    at: datetime

    @model_validator(mode="after")
    def validate_timezone(self) -> "AutonomousTransition":
        if self.at.utcoffset() is None:
            raise ValueError("transition timestamps must include a timezone")
        return self


class AutonomousRunReceipt(StrictModel):
    """External durable metadata for one selected task execution."""

    schema_version: Literal[1] = 1
    intake_receipt_path: NonEmptyStrictString
    repository: NonEmptyStrictString
    github_repository: NonEmptyStrictString
    task_id: NonEmptyStrictString
    issue_number: int = Field(strict=True, gt=0)
    issue_url: NonEmptyStrictString
    task_title: NonEmptyStrictString
    base_branch: NonEmptyStrictString
    base_commit_sha: NonEmptyStrictString
    task_status: AutonomousTaskStatus
    transitions: list[AutonomousTransition] = Field(min_length=1)
    model_calls: int = Field(default=0, strict=True, ge=0, le=5)
    branch: str | None = None
    worktree_path: str | None = None
    promotion_status: str | None = None
    delivery_status: str | None = None
    commit_sha: str | None = None
    pr_number: int | None = Field(default=None, strict=True, gt=0)
    pr_url: str | None = None
    observation_status: str | None = None
    started_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> "AutonomousRunReceipt":
        if self.started_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("receipt timestamps must include a timezone")
        if self.updated_at < self.started_at:
            raise ValueError("receipt updated_at cannot precede started_at")
        if self.transitions[-1].state != self.task_status:
            raise ValueError("last transition must match task_status")
        return self


class AutonomousRunResult(StrictModel):
    """Terminal outcome of a manually triggered, at-most-one-task run."""

    repository: NonEmptyStrictString
    github_repository: str | None = None
    issue_number: int | None = Field(default=None, strict=True, gt=0)
    issue_url: str | None = None
    task_id: str | None = None
    task_title: str | None = None
    priority: Priority | None = None
    branch: str | None = None
    task_status: AutonomousTaskStatus | None = None
    workflow_status: str | None = None
    implementation_status: str | None = None
    promotion_status: str | None = None
    delivery_status: str | None = None
    observation_status: str | None = None
    worktree_path: str | None = None
    commit_sha: str | None = None
    pr_number: int | None = Field(default=None, strict=True, gt=0)
    pr_url: str | None = None
    model_calls: int = Field(default=0, strict=True, ge=0, le=5)
    started_at: datetime
    finished_at: datetime
    status: AutonomousStatus
    stop_reason: str | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> "AutonomousRunResult":
        if self.started_at.utcoffset() is None or self.finished_at.utcoffset() is None:
            raise ValueError("run timestamps must include a timezone")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at cannot precede started_at")
        return self

    @property
    def final_status(self) -> str:
        return self.status.upper()
