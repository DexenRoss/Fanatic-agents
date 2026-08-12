"""Agent and full-flow tests for one-pass controlled implementation."""
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import pytest
from fanatic_agents.agents.implementation import ImplementationAgentService
from fanatic_agents.git.inspection import RepositorySnapshot, SnapshotFile, SnapshotTruncation
from fanatic_agents.implementation.models import ChangeOperation, ChangeSet
from fanatic_agents.implementation.service import ControlledImplementationService
from fanatic_agents.orchestrator.models import DeveloperPlan, PlannerOutput, PlannerTask, QAPlan, RepositorySnapshotMetadata, ReviewerDecision, WorkflowResult
from fanatic_agents.sandbox.errors import SandboxExecutionError
from fanatic_agents.sandbox.models import SandboxCommand, SandboxCommandResult


def snapshot() -> RepositorySnapshot:
    return RepositorySnapshot(repository_name="sample", is_git_repository=False, detected_technologies=["Python"], relevant_paths=["source.py"], files=[SnapshotFile(path="source.py", content="old\n")], truncation=SnapshotTruncation(max_relevant_files=10,max_content_files=5,max_source_content_files=2,max_characters_per_file=100,max_total_characters=500,files_considered=1,relevant_files_included=1,relevant_files_omitted=0,content_files_included=1,content_files_omitted=0,truncated_files=0,total_characters=4))


def workflow(*, status: str = "ready_for_implementation", command: SandboxCommand | None = None) -> WorkflowResult:
    task = PlannerTask(title="Update source", objective="Change behavior", rationale="Requested", acceptance_criteria=["Tests pass"], risk_level="low", requires_human_approval=False)
    return WorkflowResult(repository=RepositorySnapshotMetadata(repository_name="sample",is_git_repository=False,detached_head=False,relevant_path_count=1,content_file_count=1,snapshot_was_bounded=False), planner=PlannerOutput(repository_summary="Python",status="task_selected",selected_task=task), developer=DeveloperPlan(task_title=task.title,approach="Modify source",implementation_steps=["Edit source"],files_likely_affected=["source.py"],requires_human_approval=False), reviewer=ReviewerDecision(decision="approved",reasoning_summary="Safe"), qa=QAPlan(verification_steps=["Test"],proposed_commands=[command or SandboxCommand(argv=["python","-m","pytest"])],expected_signals=["Pass"],readiness="ready"), status=status)  # type: ignore[arg-type]


def changeset() -> ChangeSet:
    return ChangeSet(task_title="Update source",summary="Updated",changes=[ChangeOperation(operation="modify",path="source.py",content="changed\n",reason="task")])


class FakeImplementer:
    def __init__(self) -> None: self.calls = 0
    def implement(self, *_args: Any) -> ChangeSet:
        self.calls += 1
        return changeset()


class FakeSandbox:
    def __init__(self, result: SandboxCommandResult | None = None, error: Exception | None = None) -> None:
        self.calls = 0; self.seen = ""; self.result = result or command_result(); self.error = error
    def run_prepared_workspace(self, workspace, image, command, *, limits=None):
        self.calls += 1; self.seen = (workspace.path / "source.py").read_text(encoding="utf-8")
        if self.error: raise self.error
        return self.result


def command_result(*, exit_code: int | None = 0, timed_out: bool = False) -> SandboxCommandResult:
    return SandboxCommandResult(argv=["python","-m","pytest"],exit_code=exit_code,stdout="ok",stderr="",duration_seconds=0.1,timed_out=timed_out,stdout_truncated=False,stderr_truncated=False)


def repository(tmp_path: Path) -> Path:
    root=tmp_path/"repo"; root.mkdir(); (root/"source.py").write_text("old\n",encoding="utf-8"); return root


def test_implementation_agent_is_tool_free_structured_and_one_call() -> None:
    class Runner:
        calls=0
        @classmethod
        def run_sync(cls, agent, input, *, max_turns):
            cls.calls += 1; assert max_turns == 1; assert "sk-secret" not in input; return SimpleNamespace(final_output=changeset())
    service=ImplementationAgentService(runner=Runner)
    wf=workflow(); service.implement(snapshot(),wf.planner.selected_task,wf.developer,wf.reviewer,wf.qa)  # type: ignore[arg-type]
    assert service.agent.tools == [] and service.agent.output_type is ChangeSet and Runner.calls == 1


def test_success_verifies_already_modified_workspace_and_preserves_original(tmp_path: Path) -> None:
    repo=repository(tmp_path); sandbox=FakeSandbox(); implementer=FakeImplementer()
    result=ControlledImplementationService(implementer=implementer,sandbox=sandbox).run(repository=repo,snapshot=snapshot(),workflow=workflow(),image="python:3.12-slim")
    assert result.status == "verified" and result.tests_passed and result.commands_executed_count == 1
    assert sandbox.seen == "changed\n" and (repo/"source.py").read_text(encoding="utf-8") == "old\n"
    assert implementer.calls == 1 and sandbox.calls == 1


@pytest.mark.parametrize(("sandbox_result","expected"), [(command_result(exit_code=1),"verification_failed"),(command_result(exit_code=None,timed_out=True),"verification_failed")])
def test_verification_failure_stops_without_correction_loop(tmp_path: Path, sandbox_result: SandboxCommandResult, expected: str) -> None:
    implementer=FakeImplementer(); result=ControlledImplementationService(implementer=implementer,sandbox=FakeSandbox(sandbox_result)).run(repository=repository(tmp_path),snapshot=snapshot(),workflow=workflow(),image="python:3.12-slim")
    assert result.status == expected and implementer.calls == 1


def test_non_ready_workflow_never_calls_implementation_or_docker(tmp_path: Path) -> None:
    implementer=FakeImplementer(); sandbox=FakeSandbox(); result=ControlledImplementationService(implementer=implementer,sandbox=sandbox).run(repository=repository(tmp_path),snapshot=snapshot(),workflow=workflow(status="changes_requested"),image="python:3.12-slim")
    assert result.status == "implementation_failed" and implementer.calls == sandbox.calls == 0


def test_qa_command_is_revalidated_and_not_executed(tmp_path: Path) -> None:
    sandbox=FakeSandbox(); result=ControlledImplementationService(implementer=FakeImplementer(),sandbox=sandbox).run(repository=repository(tmp_path),snapshot=snapshot(),workflow=workflow(command=SandboxCommand(argv=["bash","-lc","pytest"])),image="python:3.12-slim")
    assert result.status == "policy_rejected" and sandbox.calls == 0


def test_sandbox_error_becomes_implementation_failed(tmp_path: Path) -> None:
    repo = repository(tmp_path)
    result=ControlledImplementationService(implementer=FakeImplementer(),sandbox=FakeSandbox(error=SandboxExecutionError("secret internal"))).run(repository=repo,snapshot=snapshot(),workflow=workflow(),image="python:3.12-slim")
    assert result.status == "implementation_failed" and "secret internal" not in (result.stop_reason or "")
    assert (repo / "source.py").read_text(encoding="utf-8") == "old\n"