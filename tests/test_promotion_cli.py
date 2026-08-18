"""CLI gates and rendering for verified local promotion."""



from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.git.models import BaseRepositoryState, PromotionResult
from fanatic_agents.implementation.models import (
    ChangeOperation,
    ChangeSet,
    ImplementationResult,
)
from test_implementation_flow import workflow
import re

runner = CliRunner()

ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


def plain_cli_output(output: str) -> str:
    without_ansi = ANSI_ESCAPE.sub("", output)
    return " ".join(without_ansi.split())


def project(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("# Project\n", encoding="utf-8")
    return tmp_path


def base(root: Path) -> BaseRepositoryState:
    return BaseRepositoryState(
        repository_path=str(root.resolve()),
        branch="base",
        commit_sha="a" * 40,
        working_tree_clean=True,
    )


def result(root: Path, status: str = "verified") -> ImplementationResult:
    selected = ChangeSet(
        task_title="Update source",
        summary="Verified update",
        changes=[
            ChangeOperation(
                operation="modify",
                path="source.py",
                content="changed\n",
                reason="task",
            )
        ],
    )
    return ImplementationResult(
        task="Update source",
        base_repository_state=base(root),
        changeset=selected,
        status=status,
        tests_passed=status == "verified",
        commands_executed_count=1,
    )  # type: ignore[arg-type]


def test_promote_requires_branch_and_branch_requires_promote(
    tmp_path: Path, monkeypatch
) -> None:
    root = project(tmp_path)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("workflow must not start")

    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", forbidden)
    missing = runner.invoke(
        app,
        ["workflow", "implement", str(root), "--image", "python:3.12-slim", "--promote"],
    )
    stray = runner.invoke(
        app,
        [
            "workflow",
            "implement",
            str(root),
            "--image",
            "python:3.12-slim",
            "--branch",
            "fanatic/task",
        ],
    )
    assert missing.exit_code == 2
    assert "--promote requires --branch" in plain_cli_output(missing.output)

    assert stray.exit_code == 2
    assert "--branch requires --promote" in plain_cli_output(stray.output)

    no_ai = runner.invoke(
        app,
        [
            "workflow",
            "implement",
            str(root),
            "--image",
            "python:3.12-slim",
            "--promote",
            "--branch",
            "fanatic/task",
        ],
    )
    assert no_ai.exit_code == 2
    assert "--promote requires --ai" in plain_cli_output(no_ai.output)


def test_without_promote_preserves_sprint4_and_calls_no_promotion(
    tmp_path: Path, monkeypatch
) -> None:
    root = project(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", lambda _snapshot: workflow())
    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_controlled_implementation",
        lambda *_args: result(root),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("promotion must remain opt-in")

    monkeypatch.setattr("fanatic_agents.cli.main.capture_base_repository_state", forbidden)
    monkeypatch.setattr("fanatic_agents.cli.main.promote_verified_changes", forbidden)
    invocation = runner.invoke(
        app,
        ["workflow", "implement", str(root), "--image", "python:3.12-slim", "--ai"],
    )
    assert invocation.exit_code == 0
    assert "VERIFIED" in invocation.stdout and "Verified Change Promotion" not in invocation.stdout


def test_stopped_workflow_never_calls_promotion_logic(tmp_path: Path, monkeypatch) -> None:
    root = project(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_workflow",
        lambda _snapshot: workflow(status="changes_requested"),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("promotion logic must not run")

    monkeypatch.setattr("fanatic_agents.cli.main.capture_base_repository_state", forbidden)
    monkeypatch.setattr("fanatic_agents.cli.main.promote_verified_changes", forbidden)
    invocation = runner.invoke(
        app,
        [
            "workflow",
            "implement",
            str(root),
            "--image",
            "python:3.12-slim",
            "--ai",
            "--promote",
            "--branch",
            "fanatic/task",
        ],
    )
    assert invocation.exit_code == 1 and "CHANGES_REQUESTED" in invocation.stdout


def test_failed_implementation_is_never_promoted(tmp_path: Path, monkeypatch) -> None:
    root = project(tmp_path)
    recorded = base(root)
    monkeypatch.setenv("OPENAI_API_KEY", "fake")
    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", lambda _snapshot: workflow())
    monkeypatch.setattr(
        "fanatic_agents.cli.main.capture_base_repository_state", lambda _root: recorded
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_controlled_implementation",
        lambda *_args: result(root, "verification_failed"),
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("failed implementation cannot promote")

    monkeypatch.setattr("fanatic_agents.cli.main.promote_verified_changes", forbidden)
    invocation = runner.invoke(
        app,
        [
            "workflow",
            "implement",
            str(root),
            "--image",
            "python:3.12-slim",
            "--ai",
            "--promote",
            "--branch",
            "fanatic/task",
        ],
    )
    assert invocation.exit_code == 1 and "VERIFICATION_FAILED" in invocation.stdout


def test_cli_renders_promoted_result_without_extra_model_calls(
    tmp_path: Path, monkeypatch
) -> None:
    root = project(tmp_path)
    recorded = base(root)
    calls = {"workflow": 0, "implementation": 0, "promotion": 0}
    monkeypatch.setenv("OPENAI_API_KEY", "fake")

    def fake_workflow(_snapshot):
        calls["workflow"] += 1
        return workflow()

    def fake_implementation(*args):
        calls["implementation"] += 1
        assert len(args) == 5 and args[4] == recorded
        return result(root)

    def fake_promotion(repository, implementation, branch, files):
        calls["promotion"] += 1
        assert Path(repository) == root
        assert implementation.status == "verified"
        assert branch == "fanatic/task" and files == ["source.py"]
        return PromotionResult(
            repository=str(root.resolve()),
            base_branch="base",
            base_commit="a" * 40,
            promoted_branch=branch,
            worktree_path="/promotion-worktree",
            changes=1,
            status="promoted",
        )

    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", fake_workflow)
    monkeypatch.setattr(
        "fanatic_agents.cli.main.capture_base_repository_state", lambda _root: recorded
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_controlled_implementation", fake_implementation
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.promote_verified_changes", fake_promotion
    )

    invocation = runner.invoke(
        app,
        [
            "workflow",
            "implement",
            str(root),
            "--image",
            "python:3.12-slim",
            "--ai",
            "--promote",
            "--branch",
            "fanatic/task",
        ],
    )
    assert invocation.exit_code == 0
    assert calls == {"workflow": 1, "implementation": 1, "promotion": 1}
    for expected in (
        "Fanatic Agents Verified Change Promotion",
        "PROTECTED",
        "VERIFIED",
        "APPROVED",
        "fanatic/task",
        "promotion-worktree",
        "NOT CREATED",
        "NOT PERFORMED",
        "PROMOTED",
        "The original working tree was not modified.",
    ):
        assert expected in invocation.stdout
