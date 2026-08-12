"""Strict structured contracts for controlled implementation."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel
from fanatic_agents.sandbox.models import SandboxCommandResult

MAX_CHANGED_FILES = 10
MAX_CHARACTERS_PER_FILE = 50_000
MAX_TOTAL_CHARACTERS = 150_000
MAX_DELETES = 2

ChangeOperationValue = Literal["create", "modify", "delete"]
ImplementationStatus = Literal[
    "verified",
    "verification_failed",
    "human_required",
    "policy_rejected",
    "implementation_failed",
]


class ChangeOperation(StrictModel):
    """One complete-file operation emitted by the Implementation Agent."""

    operation: ChangeOperationValue
    path: NonEmptyStrictString
    content: str | None = None
    reason: NonEmptyStrictString

    @model_validator(mode="after")
    def validate_content_for_operation(self) -> "ChangeOperation":
        if self.operation in {"create", "modify"} and self.content is None:
            raise ValueError(f"{self.operation} requires content")
        if self.operation == "delete" and self.content is not None:
            raise ValueError("delete requires content to be None")
        if self.content is not None and len(self.content) > MAX_CHARACTERS_PER_FILE:
            raise ValueError(
                f"content exceeds {MAX_CHARACTERS_PER_FILE} characters per file"
            )
        return self


class ChangeSet(StrictModel):
    """Bounded set of complete-file changes produced in one model call."""

    task_title: NonEmptyStrictString
    summary: NonEmptyStrictString
    changes: list[ChangeOperation] = Field(min_length=1, max_length=MAX_CHANGED_FILES)
    assumptions: list[NonEmptyStrictString] = Field(default_factory=list)
    implementation_notes: list[NonEmptyStrictString] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_aggregate_bounds(self) -> "ChangeSet":
        paths = [change.path for change in self.changes]
        if len(set(paths)) != len(paths):
            raise ValueError("a ChangeSet cannot operate on the same path twice")
        deletes = sum(change.operation == "delete" for change in self.changes)
        if deletes > MAX_DELETES:
            raise ValueError(f"ChangeSet exceeds maximum deletes ({MAX_DELETES})")
        characters = sum(len(change.content or "") for change in self.changes)
        if characters > MAX_TOTAL_CHARACTERS:
            raise ValueError(
                f"ChangeSet exceeds total generated characters ({MAX_TOTAL_CHARACTERS})"
            )
        return self


class AppliedChange(StrictModel):
    """Deterministic application result for one validated operation."""

    operation: ChangeOperationValue
    path: NonEmptyStrictString
    success: bool
    message: NonEmptyStrictString


class WorkspaceSummary(StrictModel):
    """Non-sensitive facts about the disposable implementation workspace."""

    original_repository_protected: bool = True
    temporary: bool = True
    initial_file_count: int = Field(strict=True, ge=0)
    initial_total_bytes: int = Field(strict=True, ge=0)
    changes_applied: int = Field(strict=True, ge=0)
    cleaned_up: bool


class ImplementationResult(StrictModel):
    """Terminal result of one non-iterative implementation phase."""

    task: NonEmptyStrictString
    changeset: ChangeSet | None = None
    applied_changes: list[AppliedChange] = Field(default_factory=list)
    verification_results: list[SandboxCommandResult] = Field(default_factory=list)
    status: ImplementationStatus
    stop_reason: str | None = None
    workspace_summary: WorkspaceSummary | None = None
    tests_passed: bool
    commands_executed_count: int = Field(strict=True, ge=0)
