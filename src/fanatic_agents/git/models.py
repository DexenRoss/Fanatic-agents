"""Structured Git state and verified promotion results."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel

PromotionStatus = Literal[
    "promoted",
    "not_verified",
    "repository_invalid",
    "detached_head",
    "repository_dirty",
    "base_changed",
    "branch_rejected",
    "branch_exists",
    "policy_rejected",
    "promotion_failed",
]


class BaseRepositoryState(StrictModel):
    """Git state recorded before controlled implementation starts."""

    repository_path: NonEmptyStrictString
    branch: NonEmptyStrictString
    commit_sha: NonEmptyStrictString
    working_tree_clean: bool


class PromotionResult(StrictModel):
    """Terminal result of one explicit, local verified-change promotion."""

    repository: NonEmptyStrictString
    base_branch: str | None = None
    base_commit: str | None = None
    promoted_branch: str | None = None
    worktree_path: str | None = None
    changes: int = Field(default=0, strict=True, ge=0)
    status: PromotionStatus
    stop_reason: str | None = None
