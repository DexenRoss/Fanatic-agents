"""Tests for explicit read-only orchestration and stop conditions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from fanatic_agents.git.inspection import (
    RepositoryInspector,
    RepositorySnapshot,
    SnapshotFile,
    SnapshotTruncation,
)
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerOutput,
    PlannerTask,
    QAPlan,
    ReviewerDecision,
)
from fanatic_agents.orchestrator.workflow import WorkflowOrchestrator
from fanatic_agents.sandbox.models import SandboxCommand


def _snapshot(*, with_context: bool = True) -> RepositorySnapshot:
    paths = ["README.md"] if with_context else []
    files = [SnapshotFile(path="README.md", content="# Sample")] if with_context else []
    return RepositorySnapshot(
        repository_name="sample",
        is_git_repository=False,
        detected_technologies=["Python"] if with_context else [],
        important_files=paths,
        relevant_paths=paths,
        files=files,
        truncation=SnapshotTruncation(
            max_relevant_files=10,
            max_content_files=5,
            max_source_content_files=2,
            max_characters_per_file=100,
            max_total_characters=500,
            files_considered=len(paths),
            relevant_files_included=len(paths),
            relevant_files_omitted=0,
            content_files_included=len(files),
            content_files_omitted=0,
            truncated_files=0,
            total_characters=sum(len(file.content) for file in files),
        ),
    )


def _task(**changes: Any) -> PlannerTask:
    data: dict[str, Any] = {
        "title": "Add a focused test",
        "objective": "Cover snapshot-visible behavior.",
        "rationale": "A test command is documented.",
        "acceptance_criteria": ["The test passes."],
        "risk_level": "low",
        "requires_human_approval": False,
        "assumptions": [],
    }
    data.update(changes)
    return PlannerTask(**data)


def _planner(task: PlannerTask | None = None) -> PlannerOutput:
    return PlannerOutput(
        repository_summary="A bounded Python project.",
        status="task_selected",
        selected_task=task or _task(),
    )


def _developer(**changes: Any) -> DeveloperPlan:
    data: dict[str, Any] = {
        "task_title": "Add a focused test",
        "approach": "Add one unit test.",
        "implementation_steps": ["Add the test."],
        "files_likely_affected": ["tests/test_sample.py"],
        "proposed_commands": [SandboxCommand(argv=["python", "-m", "pytest"])],
        "risks": [],
        "assumptions": [],
        "requires_human_approval": False,
    }
    data.update(changes)
    return DeveloperPlan(**data)


def _review(decision: str = "approved") -> ReviewerDecision:
    return ReviewerDecision(
        decision=decision,  # type: ignore[arg-type]
        reasoning_summary="One-pass review completed.",
    )


def _qa(**changes: Any) -> QAPlan:
    data: dict[str, Any] = {
        "verification_steps": ["Run tests manually in the future."],
        "proposed_commands": [SandboxCommand(argv=["python", "-m", "pytest"])],
        "expected_signals": ["All tests pass."],
        "risks": [],
        "readiness": "ready",
    }
    data.update(changes)
    return QAPlan(**data)


class FakePlanner:
    def __init__(self, output: PlannerOutput, calls: list[str]) -> None:
        self.output = output
        self.calls = calls

    def plan(self, _snapshot: RepositorySnapshot) -> PlannerOutput:
        self.calls.append("planner")
        return self.output


class FakeDeveloper:
    def __init__(self, output: DeveloperPlan, calls: list[str]) -> None:
        self.output = output
        self.calls = calls

    def plan(self, _snapshot: RepositorySnapshot, _task: PlannerTask) -> DeveloperPlan:
        self.calls.append("developer")
        return self.output


class FakeReviewer:
    def __init__(self, output: ReviewerDecision, calls: list[str]) -> None:
        self.output = output
        self.calls = calls

    def review(
        self, _snapshot: RepositorySnapshot, _task: PlannerTask, _plan: DeveloperPlan
    ) -> ReviewerDecision:
        self.calls.append("reviewer")
        return self.output


class FakeQA:
    def __init__(self, output: QAPlan, calls: list[str]) -> None:
        self.output = output
        self.calls = calls

    def plan(
        self,
        _snapshot: RepositorySnapshot,
        _task: PlannerTask,
        _developer: DeveloperPlan,
        _reviewer: ReviewerDecision,
    ) -> QAPlan:
        self.calls.append("qa")
        return self.output


def _orchestrator(
    *,
    planner: PlannerOutput | None = None,
    developer: DeveloperPlan | None = None,
    reviewer: ReviewerDecision | None = None,
    qa: QAPlan | None = None,
) -> tuple[WorkflowOrchestrator, list[str]]:
    calls: list[str] = []
    return (
        WorkflowOrchestrator(
            planner=FakePlanner(planner or _planner(), calls),
            developer=FakeDeveloper(developer or _developer(), calls),
            reviewer=FakeReviewer(reviewer or _review(), calls),
            qa=FakeQA(qa or _qa(), calls),
        ),
        calls,
    )


def test_complete_workflow_runs_in_order_with_at_most_four_calls() -> None:
    orchestrator, calls = _orchestrator()

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer", "reviewer", "qa"]
    assert len(calls) == 4
    assert result.status == "ready_for_implementation"
    assert result.developer_command_validations[0].valid is True
    assert result.qa_command_validations[0].valid is True


@pytest.mark.parametrize(
    "task",
    [
        _task(requires_human_approval=True),
        _task(risk_level="high"),
    ],
)
def test_planner_human_gates_stop_developer(task: PlannerTask) -> None:
    orchestrator, calls = _orchestrator(planner=_planner(task))

    result = orchestrator.run(_snapshot())

    assert calls == ["planner"]
    assert result.status == "human_required"


def test_developer_human_gate_stops_reviewer() -> None:
    orchestrator, calls = _orchestrator(
        developer=_developer(requires_human_approval=True)
    )

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer"]
    assert result.status == "human_required"


@pytest.mark.parametrize(
    "command",
    [
        SandboxCommand(argv=["bash", "-lc", "pytest"]),
        SandboxCommand(argv=["python", "-c", "print('unsafe')"]),
        SandboxCommand(argv=["docker", "run", "image"]),
        SandboxCommand(argv=["python", "-m", "pytest", "&&", "echo"]),
    ],
)
def test_invalid_developer_command_stops_reviewer(command: SandboxCommand) -> None:
    orchestrator, calls = _orchestrator(
        developer=_developer(proposed_commands=[command])
    )

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer"]
    assert result.status == "changes_requested"
    assert result.developer_command_validations[0].valid is False
    assert "no command was executed" in (result.stop_reason or "")


@pytest.mark.parametrize(
    ("decision", "status"),
    [("changes_requested", "changes_requested"), ("human_required", "human_required")],
)
def test_reviewer_gate_stops_qa(decision: str, status: str) -> None:
    orchestrator, calls = _orchestrator(reviewer=_review(decision))

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer", "reviewer"]
    assert result.status == status


def test_qa_human_required_sets_final_human_status() -> None:
    orchestrator, calls = _orchestrator(qa=_qa(readiness="human_required"))

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer", "reviewer", "qa"]
    assert result.status == "human_required"


def test_invalid_qa_command_is_rejected_without_execution() -> None:
    orchestrator, calls = _orchestrator(
        qa=_qa(proposed_commands=[SandboxCommand(argv=["ssh", "example.com"])])
    )

    result = orchestrator.run(_snapshot())

    assert calls == ["planner", "developer", "reviewer", "qa"]
    assert result.status == "changes_requested"
    assert result.qa_command_validations[0].valid is False


def test_empty_snapshot_stops_without_agent_calls() -> None:
    orchestrator, calls = _orchestrator()

    result = orchestrator.run(_snapshot(with_context=False))

    assert calls == []
    assert result.status == "insufficient_context"


def test_agent_exception_is_sanitized_and_stops_workflow() -> None:
    class FailingPlanner:
        def plan(self, _snapshot: RepositorySnapshot) -> PlannerOutput:
            raise RuntimeError("sk-secret-internal-provider-message")

    orchestrator, calls = _orchestrator()
    orchestrator._planner = FailingPlanner()  # type: ignore[attr-defined]

    result = orchestrator.run(_snapshot())

    assert calls == []
    assert result.status == "failed"
    assert "sk-secret" not in (result.stop_reason or "")


def test_workflow_never_runs_docker_or_modifies_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    readme = tmp_path / "README.md"
    readme.write_text("# Untouched\n", encoding="utf-8")
    before = readme.read_bytes()

    def forbidden_execution(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("Docker sandbox must not be executed")

    monkeypatch.setattr(
        "fanatic_agents.sandbox.docker.run_sandbox_command", forbidden_execution
    )
    snapshot = RepositoryInspector().inspect(tmp_path)
    orchestrator, _ = _orchestrator()

    result = orchestrator.run(snapshot)

    assert result.status == "ready_for_implementation"
    assert readme.read_bytes() == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["README.md"]
