"""Strict, normalized contracts for read-only pull request observation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import Field

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel

CheckExecutionStatus = Literal[
    "queued",
    "pending",
    "in_progress",
    "waiting",
    "requested",
    "completed",
    "unknown",
]
CheckConclusion = Literal[
    "success",
    "failure",
    "cancelled",
    "skipped",
    "neutral",
    "timed_out",
    "action_required",
    "unknown",
]
CIState = Literal["pending", "passed", "failed", "no_ci_reported"]
ReviewState = Literal[
    "approved", "changes_requested", "review_required", "none", "unknown"
]
PullRequestState = Literal["open", "closed", "merged", "unknown"]
Mergeability = Literal["mergeable", "conflicting", "unknown"]
ObservationStatus = Literal[
    "waiting_for_ci",
    "ci_failed",
    "no_ci_reported",
    "waiting_for_review",
    "changes_requested",
    "ready_for_human_merge",
    "merge_conflict",
    "pr_draft",
    "pr_closed",
    "merged_externally",
    "pr_head_drifted",
    "invalid_delivery",
    "github_unavailable",
    "observation_failed",
]


class PullRequestCheck(StrictModel):
    """One GitHub check normalized without retaining logs or provider payloads."""

    name: NonEmptyStrictString
    context: str | None = None
    status: CheckExecutionStatus
    conclusion: CheckConclusion | None = None
    details_url: str | None = None


class PullRequestObservation(StrictModel):
    """One deterministic, secret-free snapshot of a delivered pull request."""

    schema_version: Literal[1] = 1
    repository: NonEmptyStrictString
    promotion_worktree: NonEmptyStrictString
    pr_number: int | None = Field(default=None, strict=True, gt=0)
    pr_url: str | None = None
    base_branch: str | None = None
    head_branch: str | None = None
    expected_head_sha: str | None = None
    observed_head_sha: str | None = None
    pr_state: PullRequestState = "unknown"
    is_draft: bool = False
    mergeable: Mergeability = "unknown"
    review_state: ReviewState = "unknown"
    approvals: int = Field(default=0, strict=True, ge=0)
    changes_requested: int = Field(default=0, strict=True, ge=0)
    checks: list[PullRequestCheck] = Field(default_factory=list)
    ci_state: CIState = "no_ci_reported"
    status: ObservationStatus
    stop_reason: str | None = None
    observed_at: datetime

    @property
    def final_status(self) -> str:
        """Return an explicit display status that preserves the human merge gate."""
        return self.status.upper()
