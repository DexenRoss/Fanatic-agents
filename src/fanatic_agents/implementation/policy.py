"""Deny-by-default deterministic policy for complete-file changes."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import Field

from fanatic_agents.core.path_safety import is_excluded_directory, is_secret_path
from fanatic_agents.core.project import StrictModel
from fanatic_agents.implementation.models import ChangeOperation, ChangeSet

PolicyStatus = Literal["approved", "human_required", "rejected"]
WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")
PROTECTED_EXACT = frozenset({"agents.md"})
PROTECTED_PREFIXES = (
    ".github/workflows/",
    ".ssh/",
    "deploy/",
    "deployment/",
    "infra/",
    "terraform/",
)
PROTECTED_NAMES = frozenset({
    "docker-compose.yml",
    "docker-compose.yaml",
    "compose.yml",
    "compose.yaml",
})
SENSITIVE_SCOPE_TOKENS = frozenset({"auth", "authentication", "migration", "migrations"})


class ChangePolicyIssue(StrictModel):
    """One deterministic policy finding."""

    path: str
    status: Literal["human_required", "rejected"]
    reason: str


class ChangePolicyResult(StrictModel):
    """Atomic validation outcome for a complete ChangeSet."""

    status: PolicyStatus
    issues: list[ChangePolicyIssue] = Field(default_factory=list)


class ChangePolicy:
    """Validate every operation before any workspace mutation occurs."""

    def validate(
        self,
        changeset: ChangeSet,
        *,
        workspace: Path,
        files_likely_affected: list[str],
    ) -> ChangePolicyResult:
        root = Path(workspace).resolve(strict=True)
        issues: list[ChangePolicyIssue] = []
        for change in changeset.changes:
            issue = self._validate_change(
                change,
                root=root,
                files_likely_affected=files_likely_affected,
            )
            if issue is not None:
                issues.append(issue)
        if any(issue.status == "rejected" for issue in issues):
            return ChangePolicyResult(status="rejected", issues=issues)
        if issues:
            return ChangePolicyResult(status="human_required", issues=issues)
        return ChangePolicyResult(status="approved")

    def _validate_change(
        self,
        change: ChangeOperation,
        *,
        root: Path,
        files_likely_affected: list[str],
    ) -> ChangePolicyIssue | None:
        relative, error = _safe_relative_path(change.path)
        if error is not None:
            return ChangePolicyIssue(path=change.path, status="rejected", reason=error)
        assert relative is not None
        normalized = relative.as_posix()
        lowered = normalized.lower()

        if any(is_excluded_directory(part) for part in relative.parts):
            return _rejected(change.path, "Excluded repository directories cannot be changed.")
        if is_secret_path(relative):
            return _rejected(change.path, "Secret or credential paths cannot be changed.")
        if lowered == "docker.sock" or lowered.endswith("/docker.sock"):
            return _rejected(change.path, "The Docker socket cannot be changed.")
        if _is_protected(lowered):
            return _human(change.path, "The path is protected and requires human approval.")
        if any(token in {part.lower() for part in relative.parts} for token in SENSITIVE_SCOPE_TOKENS):
            return _human(change.path, "Sensitive authentication or migration changes require a human.")
        if not _is_in_scope(relative, files_likely_affected):
            return _human(change.path, "The path is outside DeveloperPlan.files_likely_affected.")

        target = root.joinpath(*relative.parts)
        existing_parent = target.parent
        while not existing_parent.exists() and existing_parent != root:
            existing_parent = existing_parent.parent
        try:
            existing_parent.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            return _rejected(change.path, "The target parent is unsafe or outside the workspace.")
        if target.is_symlink() or _has_symlink_component(root, relative):
            return _rejected(change.path, "Symlink targets cannot be changed.")
        if change.operation == "create" and target.exists():
            return _rejected(change.path, "Create target already exists.")
        if change.operation in {"modify", "delete"}:
            if not target.exists():
                return _rejected(change.path, f"{change.operation.title()} target does not exist.")
            if not target.is_file():
                return _rejected(change.path, "Only regular files can be modified or deleted.")
        if change.operation == "delete" and _delete_requires_human(relative):
            return _human(change.path, "This delete is potentially risky and requires a human.")
        return None


def _safe_relative_path(value: str) -> tuple[PurePosixPath | None, str | None]:
    if "\x00" in value or "\\" in value:
        return None, "Paths must be normalized relative POSIX paths."
    if value.startswith("/") or WINDOWS_ABSOLUTE.match(value):
        return None, "Absolute paths are not allowed."
    path = PurePosixPath(value)
    if not path.parts or path.as_posix() in {"", "."}:
        return None, "A file path is required."
    if any(part in {"..", "."} for part in path.parts):
        return None, "Path traversal is not allowed."
    return path, None


def _is_protected(lowered: str) -> bool:
    return (
        lowered in PROTECTED_EXACT
        or lowered in PROTECTED_NAMES
        or any(lowered.startswith(prefix) for prefix in PROTECTED_PREFIXES)
        or lowered.endswith((".tf", ".tfvars"))
    )


def _is_in_scope(path: PurePosixPath, planned_paths: list[str]) -> bool:
    normalized_plans: list[PurePosixPath] = []
    for value in planned_paths:
        planned, error = _safe_relative_path(value)
        if error is None and planned is not None:
            normalized_plans.append(planned)
    if path in normalized_plans:
        return True
    if not normalized_plans:
        return False
    if path.parts and path.parts[0].lower() in {"test", "tests"}:
        path_stem = path.stem.removeprefix("test_").removesuffix("_test")
        return any(planned.stem == path_stem for planned in normalized_plans)
    return False


def _has_symlink_component(root: Path, relative: PurePosixPath) -> bool:
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _delete_requires_human(path: PurePosixPath) -> bool:
    return path.suffix.lower() in {".sql", ".db", ".sqlite", ".sqlite3"}


def _rejected(path: str, reason: str) -> ChangePolicyIssue:
    return ChangePolicyIssue(path=path, status="rejected", reason=reason)


def _human(path: str, reason: str) -> ChangePolicyIssue:
    return ChangePolicyIssue(path=path, status="human_required", reason=reason)
