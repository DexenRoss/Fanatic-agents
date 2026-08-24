"""Tests for Sprint 3 structured contracts and tool-free agents."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from fanatic_agents.agents._shared import AgentExecutionError
from fanatic_agents.agents.developer_planning import DeveloperPlanningAgentService
from fanatic_agents.agents.planner import PlannerAgentService
from fanatic_agents.agents.qa import QAAgentService
from fanatic_agents.agents.reviewer import ReviewerAgentService
from fanatic_agents.git.inspection import (
    RepositorySnapshot,
    SnapshotFile,
    SnapshotTruncation,
)
from fanatic_agents.intake.models import TaskSpec
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerOutput,
    PlannerTask,
    QAPlan,
    ReviewerDecision,
)
from fanatic_agents.sandbox.models import SandboxCommand


def _snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(
        repository_name="sample",
        is_git_repository=True,
        current_branch="feature/test",
        working_tree_clean=True,
        detected_technologies=["Python"],
        important_files=["pyproject.toml"],
        relevant_paths=["pyproject.toml"],
        inferred_test_commands=["python -m pytest"],
        files=[SnapshotFile(path="pyproject.toml", content="[project]\nname='sample'")],
        truncation=SnapshotTruncation(
            max_relevant_files=10,
            max_content_files=5,
            max_source_content_files=2,
            max_characters_per_file=100,
            max_total_characters=500,
            files_considered=1,
            relevant_files_included=1,
            relevant_files_omitted=0,
            content_files_included=1,
            content_files_omitted=0,
            truncated_files=0,
            total_characters=25,
        ),
    )


def _task() -> PlannerTask:
    return PlannerTask(
        title="Add one focused test",
        objective="Cover the documented behavior.",
        rationale="The snapshot shows a Python test suite.",
        acceptance_criteria=["The focused test passes."],
        risk_level="low",
        requires_human_approval=False,
        assumptions=["Only snapshot-visible behavior is in scope."],
    )


def _planner_output() -> PlannerOutput:
    return PlannerOutput(
        repository_summary="A bounded Python project.",
        status="task_selected",
        selected_task=_task(),
        planning_notes=["One task selected."],
    )


def _task_spec() -> TaskSpec:
    return TaskSpec(
        task_id="github:owner/repo#8",
        repository="/repo",
        issue_number=8,
        issue_url="https://github.com/owner/repo/issues/8",
        title="Original Issue title",
        description="Implement the bounded Issue.",
        labels=["fanatic:ready"],
        priority="none",
        base_branch="main",
        base_commit_sha="a" * 40,
        selected_at=datetime(2026, 8, 24, tzinfo=UTC),
    )


def _developer_plan() -> DeveloperPlan:
    return DeveloperPlan(
        task_title=_task().title,
        approach="Add a focused unit test without changing architecture.",
        implementation_steps=["Add one test case."],
        files_likely_affected=["tests/test_sample.py"],
        proposed_commands=[SandboxCommand(argv=["python", "-m", "pytest"])],
        risks=["Snapshot context is bounded."],
        requires_human_approval=False,
    )


def _review() -> ReviewerDecision:
    return ReviewerDecision(
        decision="approved",
        reasoning_summary="The plan is small and testable.",
    )


def _qa_plan() -> QAPlan:
    return QAPlan(
        verification_steps=["Run the focused test suite manually later."],
        proposed_commands=[SandboxCommand(argv=["python", "-m", "pytest"])],
        expected_signals=["All tests pass."],
        readiness="ready",
    )


class FakeRunner:
    def __init__(self, output: Any) -> None:
        self.output = output
        self.calls: list[tuple[Any, str, int]] = []

    def run_sync(
        self, starting_agent: Any, input: str, *, max_turns: int
    ) -> SimpleNamespace:
        self.calls.append((starting_agent, input, max_turns))
        return SimpleNamespace(final_output=self.output)


def test_planner_output_contains_exactly_one_task_contract() -> None:
    output = _planner_output()

    assert output.selected_task == _task()
    with pytest.raises(ValidationError):
        PlannerOutput(
            repository_summary="Summary",
            status="task_selected",
            selected_task=None,
        )
    with pytest.raises(ValidationError):
        PlannerOutput(
            repository_summary="Summary",
            status="insufficient_context",
            selected_task=_task(),
        )


def test_developer_plan_requires_structured_commands() -> None:
    assert _developer_plan().proposed_commands[0].argv == ["python", "-m", "pytest"]
    data = _developer_plan().model_dump()
    data["proposed_commands"] = ["python -m pytest"]
    with pytest.raises(ValidationError):
        DeveloperPlan.model_validate(data)


def test_reviewer_decision_is_strictly_structured() -> None:
    assert _review().decision == "approved"
    with pytest.raises(ValidationError):
        ReviewerDecision(decision="maybe", reasoning_summary="Unsupported")  # type: ignore[arg-type]


def test_qa_plan_requires_structured_commands_and_readiness() -> None:
    assert _qa_plan().readiness == "ready"
    data = _qa_plan().model_dump()
    data["readiness"] = "unknown"
    with pytest.raises(ValidationError):
        QAPlan.model_validate(data)


@pytest.mark.parametrize(
    ("service", "output_type"),
    [
        (PlannerAgentService(runner=FakeRunner(_planner_output())), PlannerOutput),
        (
            DeveloperPlanningAgentService(runner=FakeRunner(_developer_plan())),
            DeveloperPlan,
        ),
        (ReviewerAgentService(runner=FakeRunner(_review())), ReviewerDecision),
        (QAAgentService(runner=FakeRunner(_qa_plan())), QAPlan),
    ],
)
def test_sprint_three_agents_are_tool_free(service: Any, output_type: type[Any]) -> None:
    assert service.agent.tools == []
    assert service.agent.output_type is output_type


def test_each_service_uses_one_runner_turn() -> None:
    snapshot = _snapshot()
    task = _task()
    developer_plan = _developer_plan()
    reviewer = _review()
    cases = [
        (PlannerAgentService(runner=(runner := FakeRunner(_planner_output()))), (snapshot,)),
        (
            DeveloperPlanningAgentService(
                runner=(developer_runner := FakeRunner(developer_plan))
            ),
            (snapshot, task),
        ),
        (
            ReviewerAgentService(runner=(reviewer_runner := FakeRunner(reviewer))),
            (snapshot, task, developer_plan),
        ),
        (
            QAAgentService(runner=(qa_runner := FakeRunner(_qa_plan()))),
            (snapshot, task, developer_plan, reviewer),
        ),
    ]

    for service, arguments in cases:
        if isinstance(service, ReviewerAgentService):
            service.review(*arguments)
        else:
            service.plan(*arguments)

    for used_runner in (runner, developer_runner, reviewer_runner, qa_runner):
        assert len(used_runner.calls) == 1
        assert used_runner.calls[0][2] == 1


def test_task_aware_planner_prompt_requires_verbatim_source_task_id() -> None:
    task_spec = _task_spec()
    output = _planner_output().model_copy(
        update={"source_task_id": task_spec.task_id}
    )
    runner = FakeRunner(output)

    PlannerAgentService(runner=runner).plan(_snapshot(), task_spec)

    prompt = runner.calls[0][1]
    assert "Copy TaskSpec.task_id exactly and verbatim" in prompt
    assert "PlannerOutput.source_task_id" in prompt
    assert repr(task_spec.task_id) in prompt


def test_agent_exception_is_sanitized() -> None:
    class FailingRunner:
        def run_sync(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("sk-secret-provider-detail")

    with pytest.raises(AgentExecutionError) as error:
        PlannerAgentService(runner=FailingRunner()).plan(_snapshot())

    assert "sk-secret-provider-detail" not in str(error.value)
