"""Safe domain errors for local Git promotion operations."""

from __future__ import annotations

from fanatic_agents.git.models import PromotionStatus


class GitPromotionError(RuntimeError):
    """A Git promotion operation failed without exposing command output."""


class RepositoryStateError(GitPromotionError):
    """The source path cannot provide an acceptable base repository state."""

    def __init__(self, status: PromotionStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class GitCommandError(GitPromotionError):
    """A bounded Git subprocess could not complete safely."""
