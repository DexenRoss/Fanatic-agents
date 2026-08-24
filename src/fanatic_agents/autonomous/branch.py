"""Deterministic, non-LLM branch naming and collision checks."""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Literal

from fanatic_agents.git.worktree import GitRunner

MAX_BRANCH_LENGTH = 100
NON_ALNUM = re.compile(r"[^a-z0-9]+")
BranchAvailability = Literal["available", "exists", "unavailable", "invalid"]


def autonomous_branch_name(issue_number: int, title: str) -> str:
    """Return a bounded ASCII fanatic/* ref derived only from Issue identity."""
    if issue_number <= 0:
        raise ValueError("issue_number must be greater than zero")
    ascii_title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    slug = NON_ALNUM.sub("-", ascii_title.casefold()).strip("-")
    prefix = f"fanatic/issue-{issue_number}-"
    maximum = MAX_BRANCH_LENGTH - len(prefix)
    slug = slug[:maximum].rstrip("-") or "task"
    return prefix + slug


class BranchAvailabilityService:
    """Check local and remote ref collisions without creating or overwriting refs."""

    def __init__(self, *, git: GitRunner | None = None) -> None:
        self._git = git or GitRunner(timeout_seconds=20.0)

    def check(self, repository: Path, branch: str) -> BranchAvailability:
        valid = self._git.run(repository, "check-ref-format", "--branch", branch)
        if valid.returncode != 0:
            return "invalid"
        local = self._git.run(
            repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        if local.returncode == 0:
            return "exists"
        if local.returncode not in {1}:
            return "unavailable"
        remote = self._git.run(
            repository, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
        )
        if remote.returncode != 0:
            return "unavailable"
        return "exists" if remote.stdout.strip() else "available"
