"""Structured contracts for the read-only multi-agent workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.sandbox.models import SandboxCommand

RiskLevel = Literal["low", "medium", "high"]
PlannerStatus = Literal["task_selected", "insufficient_context"]
ReviewerDecisionValue = Literal["approved", "changes_requested", "human_required"]
QAReadiness = Literal["ready", "needs_attention", "human_required"]
WorkflowStatus = Literal[
    "ready_for_implementation",
    "human_required",
    "changes_requested",
    "insufficient_context",
    "failed",
]


class PlannerTask(StrictModel):
    """The single bounded task selected by the Planner."""

    title: NonEmptyStrictString
    objective: NonEmptyStrictString
    rationale: NonEmptyStrictString
    acceptance_criteria: list[NonEmptyStrictString] = Field(min_length=1)
    risk_level: RiskLevel
    requires_human_approval: bool
    assumptions: list[NonEmptyStrictString] = Field(default_factory=list)


class PlannerOutput(StrictModel):
    """Planner result containing at most one selected task."""

    repository_summary: NonEmptyStrictString
    status: PlannerStatus
    selected_task: PlannerTask | None = None
    planning_notes: list[NonEmptyStrictString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_selected_task(self) -> "PlannerOutput":
        if self.status == "task_selected" and self.selected_task is None:
            raise ValueError("task_selected requires selected_task")
        if self.status == "insufficient_context" and self.selected_task is not None:
            raise ValueError("insufficient_context cannot include selected_task")
        return self


class DeveloperPlan(StrictModel):
    """Read-only implementation proposal produced by Developer Planning."""

    task_title: NonEmptyStrictString
    approach: NonEmptyStrictString
    implementation_steps: list[NonEmptyStrictString] = Field(min_length=1)
    files_likely_affected: list[NonEmptyStrictString] = Field(default_factory=list)
    proposed_commands: list[SandboxCommand] = Field(default_factory=list)
    risks: list[NonEmptyStrictString] = Field(default_factory=list)
    assumptions: list[NonEmptyStrictString] = Field(default_factory=list)
    requires_human_approval: bool


class ReviewerDecision(StrictModel):
    """One-pass review decision for a proposed developer plan."""

    decision: ReviewerDecisionValue
    issues: list[NonEmptyStrictString] = Field(default_factory=list)
    required_changes: list[NonEmptyStrictString] = Field(default_factory=list)
    security_notes: list[NonEmptyStrictString] = Field(default_factory=list)
    reasoning_summary: NonEmptyStrictString


class QAPlan(StrictModel):
    """Read-only verification proposal created after reviewer approval."""

    verification_steps: list[NonEmptyStrictString] = Field(min_length=1)
    proposed_commands: list[SandboxCommand] = Field(default_factory=list)
    expected_signals: list[NonEmptyStrictString] = Field(min_length=1)
    risks: list[NonEmptyStrictString] = Field(default_factory=list)
    readiness: QAReadiness


class RepositorySnapshotMetadata(StrictModel):
    """Non-content snapshot details retained in the final result."""

    repository_name: NonEmptyStrictString
    is_git_repository: bool
    current_branch: str | None = None
    detached_head: bool
    working_tree_clean: bool | None = None
    detected_technologies: list[str] = Field(default_factory=list)
    relevant_path_count: int = Field(strict=True, ge=0)
    content_file_count: int = Field(strict=True, ge=0)
    snapshot_was_bounded: bool

    @classmethod
    def from_snapshot(cls, snapshot: RepositorySnapshot) -> "RepositorySnapshotMetadata":
        truncation = snapshot.truncation
        return cls(
            repository_name=snapshot.repository_name,
            is_git_repository=snapshot.is_git_repository,
            current_branch=snapshot.current_branch,
            detached_head=snapshot.detached_head,
            working_tree_clean=snapshot.working_tree_clean,
            detected_technologies=snapshot.detected_technologies,
            relevant_path_count=len(snapshot.relevant_paths),
            content_file_count=len(snapshot.files),
            snapshot_was_bounded=bool(
                truncation.relevant_files_omitted
                or truncation.content_files_omitted
                or truncation.truncated_files
            ),
        )


class CommandValidation(StrictModel):
    """Policy outcome for one proposed command; the command is never run."""

    command: SandboxCommand
    valid: bool
    rejection_reason: str | None = None


class WorkflowResult(StrictModel):
    """All available outputs and the terminal state of one workflow pass."""

    repository: RepositorySnapshotMetadata
    planner: PlannerOutput | None = None
    developer: DeveloperPlan | None = None
    developer_command_validations: list[CommandValidation] = Field(default_factory=list)
    reviewer: ReviewerDecision | None = None
    qa: QAPlan | None = None
    qa_command_validations: list[CommandValidation] = Field(default_factory=list)
    status: WorkflowStatus
    stop_reason: str | None = None
