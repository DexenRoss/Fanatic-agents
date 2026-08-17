"""Strict, secret-free contracts for promotion provenance and delivery."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel

DeliveryStage = Literal["promoted", "commit_created", "branch_pushed", "pr_created"]
DeliveryStatus = Literal[
    "ready",
    "delivered",
    "invalid_promotion",
    "modified_after_verification",
    "permission_denied",
    "delivery_in_progress",
    "staging_failed",
    "commit_failed",
    "remote_branch_exists",
    "push_failed",
    "pr_creation_failed",
    "github_cli_unavailable",
    "github_auth_required",
    "delivery_failed",
]


class ExpectedPromotedChange(StrictModel):
    """One path and operation bound to the verified ChangeSet."""

    path: NonEmptyStrictString
    operation: Literal["create", "modify", "delete"]
    content_sha256: str | None = None

    @model_validator(mode="after")
    def validate_hash(self) -> "ExpectedPromotedChange":
        if self.operation == "delete":
            if self.content_sha256 is not None:
                raise ValueError("deleted paths cannot have a content hash")
        elif (
            self.content_sha256 is None
            or len(self.content_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.content_sha256)
        ):
            raise ValueError("created and modified paths require a SHA-256 content hash")
        return self


class VerificationSummary(StrictModel):
    """Small non-sensitive record of one verification command result."""

    argv: list[NonEmptyStrictString]
    exit_code: int | None
    timed_out: bool
    passed: bool


class PromotionReceipt(StrictModel):
    """External provenance binding delivery to one verified promotion."""

    schema_version: Literal[1] = 1
    repository_id: NonEmptyStrictString
    repository_path: NonEmptyStrictString
    base_branch: NonEmptyStrictString
    base_commit: NonEmptyStrictString
    promoted_branch: NonEmptyStrictString
    worktree_path: NonEmptyStrictString
    task_title: NonEmptyStrictString
    expected_changes: list[ExpectedPromotedChange] = Field(min_length=1)
    implementation_status: Literal["verified"]
    promotion_status: Literal["promoted"]
    verification_summary: list[VerificationSummary] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    delivery_stage: DeliveryStage = "promoted"
    commit_sha: str | None = None
    remote: str | None = None
    remote_branch: str | None = None
    pr_number: int | None = Field(default=None, strict=True, gt=0)
    pr_url: str | None = None

    @model_validator(mode="after")
    def validate_state(self) -> "PromotionReceipt":
        paths = [change.path for change in self.expected_changes]
        if len(set(paths)) != len(paths):
            raise ValueError("receipt paths must be unique")
        if self.delivery_stage != "promoted" and self.commit_sha is None:
            raise ValueError("post-promotion delivery state requires a commit SHA")
        if self.delivery_stage in {"branch_pushed", "pr_created"}:
            if self.remote != "origin" or self.remote_branch != self.promoted_branch:
                raise ValueError("pushed delivery state must identify origin and its branch")
        if self.delivery_stage == "pr_created" and self.pr_url is None:
            raise ValueError("PR-created delivery state requires a PR URL")
        return self


class DeliveryResult(StrictModel):
    """Structured terminal result, including any permanent partial effects."""

    repository: NonEmptyStrictString
    worktree_path: NonEmptyStrictString
    base_branch: str | None = None
    base_commit: str | None = None
    branch: str | None = None
    commit_sha: str | None = None
    remote: str | None = None
    remote_branch: str | None = None
    pr_number: int | None = Field(default=None, strict=True, gt=0)
    pr_url: str | None = None
    status: DeliveryStatus
    stop_reason: str | None = None

    @property
    def final_status(self) -> str:
        """Return the human-facing success status without implying a merge."""
        return "DELIVERED_FOR_REVIEW" if self.status == "delivered" else self.status.upper()
