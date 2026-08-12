"""Deterministic, shell-free application of validated complete-file changes."""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from fanatic_agents.implementation.errors import ChangeApplicationError
from fanatic_agents.implementation.models import AppliedChange, ChangeSet


class ChangeSetApplier:
    """Apply a fully policy-approved ChangeSet inside one workspace."""

    def apply(self, changeset: ChangeSet, workspace: Path) -> list[AppliedChange]:
        root = Path(workspace).resolve(strict=True)
        applied: list[AppliedChange] = []
        for change in changeset.changes:
            relative = PurePosixPath(change.path)
            target = root.joinpath(*relative.parts)
            self._assert_safe_target(root, target)
            if change.operation == "create" and target.exists():
                raise ChangeApplicationError("Create target already exists.")
            if change.operation in {"modify", "delete"} and not target.is_file():
                raise ChangeApplicationError("Change target is not a regular file.")
            try:
                if change.operation == "create":
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(change.content or "", encoding="utf-8")
                elif change.operation == "modify":
                    target.write_text(change.content or "", encoding="utf-8")
                else:
                    target.unlink()
            except OSError:
                raise ChangeApplicationError(
                    f"Change could not be applied safely: {change.path}"
                ) from None
            applied.append(
                AppliedChange(
                    operation=change.operation,
                    path=change.path,
                    success=True,
                    message="Applied inside the temporary workspace.",
                )
            )
        return applied

    @staticmethod
    def _assert_safe_target(root: Path, target: Path) -> None:
        existing_parent = target.parent
        while not existing_parent.exists() and existing_parent != root:
            existing_parent = existing_parent.parent
        try:
            existing_parent.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            raise ChangeApplicationError("Change target escaped the temporary workspace.") from None
        current = root
        for part in target.relative_to(root).parts:
            current = current / part
            if current.is_symlink():
                raise ChangeApplicationError("Symlink change targets are not allowed.")
