"""CLI tests for read-only workflow planning."""

from pathlib import Path

from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.core.settings import ApplicationSettings
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerOutput,
    PlannerTask,
    QAPlan,
    RepositorySnapshotMetadata,
    ReviewerDecision,
    WorkflowResult,
)
from fanatic_agents.sandbox.models import SandboxCommand

runner = CliRunner()


def _result() -> WorkflowResult:
    task = PlannerTask(
        title="Add one focused test",
        objective="Cover documented behavior.",
        rationale="The repository contains tests.",
        acceptance_criteria=["The test passes."],
        risk_level="low",
        requires_human_approval=False,
    )
    planner = PlannerOutput(
        repository_summary="A Python project.",
        status="task_selected",
        selected_task=task,
    )
    developer = DeveloperPlan(
        task_title=task.title,
        approach="Add a small unit test.",
        implementation_steps=["Add one test."],
        files_likely_affected=["tests/test_sample.py"],
        proposed_commands=[SandboxCommand(argv=["python", "-m", "pytest"])],
        requires_human_approval=False,
    )
    reviewer = ReviewerDecision(
        decision="approved",
        reasoning_summary="The plan is bounded.",
    )
    qa = QAPlan(
        verification_steps=["Run tests manually later."],
        proposed_commands=[SandboxCommand(argv=["python", "-m", "pytest"])],
        expected_signals=["Tests pass."],
        readiness="ready",
    )
    return WorkflowResult(
        repository=RepositorySnapshotMetadata(
            repository_name="sample",
            is_git_repository=False,
            detached_head=False,
            detected_technologies=["Python"],
            relevant_path_count=1,
            content_file_count=1,
            snapshot_was_bounded=False,
        ),
        planner=planner,
        developer=developer,
        reviewer=reviewer,
        qa=qa,
        status="ready_for_implementation",
    )


def test_workflow_help_is_registered() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "workflow" in result.stdout


def test_workflow_without_ai_only_inspects_locally(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("workflow agents must not be called")

    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", unexpected_call)
    monkeypatch.setattr(
        "fanatic_agents.cli.main.configure_openai_sdk", unexpected_call
    )

    result = runner.invoke(app, ["workflow", "plan", str(tmp_path)])

    assert result.exit_code == 0
    assert "AI workflow:" in result.stdout
    assert "NOT REQUESTED" in result.stdout
    assert "Use --ai" in result.stdout


def test_workflow_ai_without_key_fails_before_agent_call(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("workflow agents must not be called")

    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", unexpected_call)
    monkeypatch.setattr(
        "fanatic_agents.cli.main.get_settings",
        lambda: ApplicationSettings(_env_file=None),
    )

    result = runner.invoke(app, ["workflow", "plan", str(tmp_path), "--ai"])

    assert result.exit_code == 1
    assert "requires OPENAI_API_KEY" in result.stdout
    assert "no agent was called" in result.stdout


def test_workflow_ai_renders_mocked_structured_result(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    secret = "fake-key-never-print"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    calls = []

    def fake_workflow(snapshot):
        calls.append(snapshot)
        return _result()

    monkeypatch.setattr("fanatic_agents.cli.main.run_workflow", fake_workflow)

    result = runner.invoke(app, ["workflow", "plan", str(tmp_path), "--ai"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert "Fanatic Agents Workflow" in result.stdout
    assert "Add one focused test" in result.stdout
    assert "approved" in result.stdout
    assert "ready" in result.stdout
    assert "READY_FOR_IMPLEMENTATION" in result.stdout
    assert secret not in result.stdout


def test_failed_workflow_result_returns_nonzero(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
    failed = _result().model_copy(
        update={
            "status": "failed",
            "stop_reason": "Planner Agent failed; later agents were not called.",
        }
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_workflow", lambda _snapshot: failed
    )

    result = runner.invoke(app, ["workflow", "plan", str(tmp_path), "--ai"])

    assert result.exit_code == 1
    assert "FAILED" in result.stdout
    assert "Planner Agent failed" in result.stdout
