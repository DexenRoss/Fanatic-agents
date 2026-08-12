"""Models and deterministic policy tests for controlled implementation."""
from pathlib import Path
import pytest
from pydantic import ValidationError
from fanatic_agents.implementation.models import ChangeOperation, ChangeSet
from fanatic_agents.implementation.policy import ChangePolicy


def change(operation: str = "modify", path: str = "src/app.py", content: str | None = "new\n") -> ChangeOperation:
    return ChangeOperation(operation=operation, path=path, content=content, reason="Required by task")


def changeset(*changes: ChangeOperation) -> ChangeSet:
    return ChangeSet(task_title="Focused task", summary="Bounded changes", changes=list(changes))


@pytest.mark.parametrize(("operation", "content"), [("create", "x"), ("modify", "x"), ("delete", None)])
def test_change_operation_valid_invariants(operation: str, content: str | None) -> None:
    assert change(operation, content=content).operation == operation


@pytest.mark.parametrize(("operation", "content"), [("create", None), ("modify", None), ("delete", "x")])
def test_change_operation_rejects_invalid_content(operation: str, content: str | None) -> None:
    with pytest.raises(ValidationError):
        change(operation, content=content)


def test_changeset_enforces_bounds() -> None:
    with pytest.raises(ValidationError, match="maximum deletes"):
        changeset(*(change("delete", f"src/{index}.py", None) for index in range(3)))
    with pytest.raises(ValidationError):
        changeset(*(change("create", f"src/{index}.py") for index in range(11)))
    with pytest.raises(ValidationError, match="characters per file"):
        change(content="x" * 50_001)


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "src/app.py").write_text("old\n", encoding="utf-8")
    return root


def validate(tmp_path: Path, item: ChangeOperation, scope: list[str] | None = None):
    return ChangePolicy().validate(changeset(item), workspace=workspace(tmp_path), files_likely_affected=scope or ["src/app.py"])


def test_policy_allows_normal_scoped_path(tmp_path: Path) -> None:
    assert validate(tmp_path, change()).status == "approved"


@pytest.mark.parametrize("path", ["/tmp/x", "../x", "src/../x", r"C:\\temp\\x", ".env", ".git/config", "api-key.txt", "private_key.pem", "docker.sock"])
def test_policy_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    item = change("create", path, "x")
    assert validate(tmp_path, item, [path]).status == "rejected"


@pytest.mark.parametrize("path", ["AGENTS.md", ".github/workflows/ci.yml", "deploy/prod.yml", "main.tf"])
def test_policy_protected_paths_require_human(tmp_path: Path, path: str) -> None:
    item = change("create", path, "x")
    assert validate(tmp_path, item, [path]).status == "human_required"


def test_policy_rejects_symlink_and_out_of_scope(tmp_path: Path) -> None:
    root = workspace(tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("safe", encoding="utf-8")
    (root / "src/link.py").symlink_to(outside)
    linked = ChangePolicy().validate(changeset(change("modify", "src/link.py", "bad")), workspace=root, files_likely_affected=["src/link.py"])
    scoped = ChangePolicy().validate(changeset(change()), workspace=root, files_likely_affected=["src/other.py"])
    assert linked.status == "rejected"
    assert scoped.status == "human_required"