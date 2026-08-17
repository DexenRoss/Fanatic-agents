"""Narrow Git CLI boundary for dedicated promotion worktrees."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from fanatic_agents.git.errors import GitCommandError, RepositoryStateError
from fanatic_agents.git.models import BaseRepositoryState

GIT_TIMEOUT_SECONDS = 10.0
MAX_GIT_OUTPUT = 20_000


class GitRunner:
    """Execute bounded Git argv without a shell and sanitize failures."""

    def __init__(self, *, timeout_seconds: float = GIT_TIMEOUT_SECONDS) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self._timeout_seconds = timeout_seconds

    def run(
        self, repository: Path, *arguments: str
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", *arguments],
                cwd=repository,
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=self._timeout_seconds,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise GitCommandError("Git command could not complete safely.") from exc
        return subprocess.CompletedProcess(
            result.args,
            result.returncode,
            result.stdout[:MAX_GIT_OUTPUT],
            result.stderr[:MAX_GIT_OUTPUT],
        )


class RepositoryStateReader:
    """Capture the exact local branch, commit, and cleanliness of a Git root."""

    def __init__(self, *, git: GitRunner | None = None) -> None:
        self._git = git or GitRunner()

    def capture(self, repository: Path) -> BaseRepositoryState:
        path = Path(repository).expanduser()
        if path.is_symlink() or not path.is_dir():
            raise RepositoryStateError(
                "repository_invalid", "Promotion requires a valid Git repository directory."
            )
        try:
            root = path.resolve(strict=True)
        except OSError as exc:
            raise RepositoryStateError(
                "repository_invalid", "Promotion requires a valid Git repository directory."
            ) from exc

        inside = self._git.run(root, "rev-parse", "--is-inside-work-tree")
        top_level = self._git.run(root, "rev-parse", "--show-toplevel")
        if (
            inside.returncode != 0
            or inside.stdout.strip() != "true"
            or top_level.returncode != 0
        ):
            raise RepositoryStateError(
                "repository_invalid", "Promotion requires a valid Git repository."
            )
        try:
            git_root = Path(top_level.stdout.strip()).resolve(strict=True)
        except OSError as exc:
            raise RepositoryStateError(
                "repository_invalid", "The Git repository root could not be resolved safely."
            ) from exc
        if git_root != root:
            raise RepositoryStateError(
                "repository_invalid", "Promotion must be started from the Git repository root."
            )

        branch_result = self._git.run(root, "symbolic-ref", "--quiet", "--short", "HEAD")
        if branch_result.returncode != 0 or not branch_result.stdout.strip():
            raise RepositoryStateError(
                "detached_head", "Promotion is not allowed from a detached HEAD."
            )
        commit_result = self._git.run(root, "rev-parse", "--verify", "HEAD")
        if commit_result.returncode != 0 or not commit_result.stdout.strip():
            raise RepositoryStateError(
                "repository_invalid", "Promotion requires a repository with a valid HEAD commit."
            )
        status_result = self._git.run(
            root, "status", "--porcelain", "--untracked-files=normal"
        )
        if status_result.returncode != 0:
            raise RepositoryStateError(
                "repository_invalid", "The repository working tree could not be inspected."
            )
        return BaseRepositoryState(
            repository_path=str(root),
            branch=branch_result.stdout.strip(),
            commit_sha=commit_result.stdout.strip(),
            working_tree_clean=not bool(status_result.stdout.strip()),
        )


class PromotionWorktree:
    """Create and inspect one new branch in one dedicated worktree."""

    def __init__(self, repository: Path, *, git: GitRunner | None = None) -> None:
        self.repository = Path(repository).resolve(strict=True)
        self._git = git or GitRunner()

    def validate_branch(self, branch: str) -> bool:
        result = self._git.run(self.repository, "check-ref-format", "--branch", branch)
        return result.returncode == 0

    def branch_exists(self, branch: str) -> bool:
        result = self._git.run(
            self.repository, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
        )
        if result.returncode not in {0, 1}:
            raise GitCommandError("Existing local branches could not be inspected safely.")
        return result.returncode == 0

    def create(self, branch: str, destination: Path, base_commit: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.mkdir(exist_ok=False)
        result = self._git.run(
            self.repository,
            "worktree",
            "add",
            "-b",
            branch,
            str(destination),
            base_commit,
        )
        if result.returncode != 0:
            raise GitCommandError("The promotion worktree could not be created safely.")

    def changed_paths(self, destination: Path) -> set[str]:
        result = self._git.run(
            destination,
            "status",
            "--porcelain",
            "-z",
            "--untracked-files=all",
        )
        if result.returncode != 0:
            raise GitCommandError("Promoted paths could not be inspected safely.")
        paths: set[str] = set()
        records = result.stdout.split("\0")
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            if len(record) < 4:
                raise GitCommandError("Git returned an unexpected worktree status.")
            path = record[3:]
            if record[:2] in {"R ", "C ", " R", " C"}:
                if index >= len(records) or not records[index]:
                    raise GitCommandError("Git returned an unexpected worktree status.")
                path = records[index]
                index += 1
            paths.add(path)
        return paths

    def commit(self, destination: Path) -> str:
        result = self._git.run(destination, "rev-parse", "--verify", "HEAD")
        if result.returncode != 0 or not result.stdout.strip():
            raise GitCommandError("The promotion worktree commit could not be inspected.")
        return result.stdout.strip()

    def branch_commit(self, branch: str) -> str:
        result = self._git.run(
            self.repository, "rev-parse", "--verify", f"refs/heads/{branch}"
        )
        if result.returncode != 0 or not result.stdout.strip():
            raise GitCommandError("The promoted branch commit could not be inspected.")
        return result.stdout.strip()

    def rollback_failed_promotion(self, branch: str, destination: Path) -> None:
        """Remove only the worktree and new branch owned by this failed operation."""
        remove = self._git.run(
            self.repository, "worktree", "remove", "--force", str(destination)
        )
        if remove.returncode != 0 and destination.exists():
            shutil.rmtree(destination, ignore_errors=True)
            self._git.run(self.repository, "worktree", "prune")
        if destination.exists():
            raise GitCommandError("The failed promotion worktree could not be removed.")
        if self.branch_exists(branch):
            deleted = self._git.run(self.repository, "branch", "-d", branch)
            if deleted.returncode != 0 or self.branch_exists(branch):
                raise GitCommandError("The failed promotion branch could not be removed.")
        self._remove_empty_parents(destination.parent)

    def _remove_empty_parents(self, directory: Path) -> None:
        worktree_root = self.repository.parent / ".fanatic-agents-worktrees"
        current = directory
        while current != self.repository.parent and current.is_relative_to(worktree_root):
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent
