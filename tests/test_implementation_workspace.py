"""Temporary workspace isolation and deterministic application tests."""
from pathlib import Path
import pytest
from fanatic_agents.implementation.apply import ChangeSetApplier
from fanatic_agents.implementation.models import ChangeOperation, ChangeSet
from fanatic_agents.implementation.policy import ChangePolicy
from fanatic_agents.implementation.workspace import TemporaryImplementationWorkspace


def cs(*items: ChangeOperation) -> ChangeSet:
    return ChangeSet(task_title="task", summary="summary", changes=list(items))


def op(operation: str, path: str, content: str | None) -> ChangeOperation:
    return ChangeOperation(operation=operation, path=path, content=content, reason="task")


def test_modify_create_delete_only_affect_temporary_copy(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "modify.txt").write_text("original", encoding="utf-8")
    (repository / "delete.txt").write_text("keep", encoding="utf-8")
    changes = cs(op("modify", "modify.txt", "changed"), op("create", "new.txt", "new"), op("delete", "delete.txt", None))
    captured: Path | None = None
    with TemporaryImplementationWorkspace(repository) as prepared:
        captured = prepared.path
        assert ChangePolicy().validate(changes, workspace=prepared.path, files_likely_affected=["modify.txt", "new.txt", "delete.txt"]).status == "approved"
        ChangeSetApplier().apply(changes, prepared.path)
        assert (prepared.path / "modify.txt").read_text(encoding="utf-8") == "changed"
        assert (prepared.path / "new.txt").read_text(encoding="utf-8") == "new"
        assert not (prepared.path / "delete.txt").exists()
    assert captured is not None and not captured.exists()
    assert (repository / "modify.txt").read_text(encoding="utf-8") == "original"
    assert (repository / "delete.txt").read_text(encoding="utf-8") == "keep"
    assert not (repository / "new.txt").exists()


def test_policy_failure_applies_nothing_and_original_stays_intact(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    original = repository / "source.txt"
    original.write_text("original", encoding="utf-8")
    changes = cs(op("modify", "source.txt", "changed"), op("create", "../escape.txt", "bad"))
    with TemporaryImplementationWorkspace(repository) as prepared:
        result = ChangePolicy().validate(changes, workspace=prepared.path, files_likely_affected=["source.txt", "../escape.txt"])
        assert result.status == "rejected"
        assert (prepared.path / "source.txt").read_text(encoding="utf-8") == "original"
    assert original.read_text(encoding="utf-8") == "original"
    assert not (tmp_path / "escape.txt").exists()


def test_workspace_copy_does_not_follow_symlink_escape(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (repository / "link.txt").symlink_to(outside)
    with TemporaryImplementationWorkspace(repository) as prepared:
        assert not (prepared.path / "link.txt").exists()
    assert outside.read_text(encoding="utf-8") == "outside"