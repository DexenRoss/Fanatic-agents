"""Single-pass controller for controlled implementation and verification."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from fanatic_agents.agents.implementation import ImplementationAgentService
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.git.models import BaseRepositoryState
from fanatic_agents.implementation.apply import ChangeSetApplier
from fanatic_agents.implementation.errors import ChangeApplicationError
from fanatic_agents.implementation.models import (
    AppliedChange,
    ChangeSet,
    ImplementationResult,
    changeset_sha256,
    ImplementationStatus,
    WorkspaceSummary,
)
from fanatic_agents.intake.models import TaskSpec
from fanatic_agents.implementation.policy import ChangePolicy
from fanatic_agents.implementation.verification import PreparedWorkspaceSandbox
from fanatic_agents.implementation.workspace import TemporaryImplementationWorkspace
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerTask,
    QAPlan,
    ReviewerDecision,
    WorkflowResult,
)
from fanatic_agents.sandbox.errors import SandboxError, SandboxPolicyError
from fanatic_agents.sandbox.models import SandboxCommand, SandboxCommandResult, SandboxLimits
from fanatic_agents.sandbox.policy import CommandPolicy, validate_image_reference
from fanatic_agents.sandbox.workspace import PreparedWorkspace


class Implementer(Protocol):
    def implement(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        developer_plan: DeveloperPlan,
        reviewer: ReviewerDecision,
        qa: QAPlan,
        task_spec: TaskSpec | None = None,
    ) -> ChangeSet: ...


class PreparedWorkspaceVerifier(Protocol):
    def run_prepared_workspace(
        self,
        workspace: PreparedWorkspace,
        image: str,
        command: SandboxCommand,
        *,
        limits: SandboxLimits | None = None,
    ) -> SandboxCommandResult: ...


class ControlledImplementationService:
    """Generate, validate, apply, and verify exactly once in a temporary copy."""

    def __init__(
        self,
        *,
        implementer: Implementer | None = None,
        change_policy: ChangePolicy | None = None,
        command_policy: CommandPolicy | None = None,
        applier: ChangeSetApplier | None = None,
        sandbox: PreparedWorkspaceVerifier | None = None,
    ) -> None:
        self._implementer = implementer or ImplementationAgentService()
        self._change_policy = change_policy or ChangePolicy()
        self._command_policy = command_policy or CommandPolicy()
        self._applier = applier or ChangeSetApplier()
        self._sandbox = sandbox or PreparedWorkspaceSandbox()

    def run(
        self,
        *,
        repository: Path,
        snapshot: RepositorySnapshot,
        workflow: WorkflowResult,
        image: str,
        base_repository_state: BaseRepositoryState | None = None,
    ) -> ImplementationResult:
        """Run one implementation call only after every Sprint 3 gate passed."""
        task = _task_title(workflow)
        approved = _approved_contracts(workflow)
        if approved is None:
            status = (
                "human_required"
                if workflow.status == "human_required"
                else "implementation_failed"
            )
            return _result_without_workspace(
                task,
                status=status,
                reason="Implementation requires a workflow with status ready_for_implementation.",
                base_repository_state=base_repository_state,
            )
        planner_task, developer_plan, reviewer, qa = approved

        try:
            validated_image = validate_image_reference(image)
        except SandboxPolicyError as exc:
            return _result_without_workspace(
                task,
                status="policy_rejected",
                reason=str(exc),
                base_repository_state=base_repository_state,
            )

        try:
            changeset = (
                self._implementer.implement(
                    snapshot, planner_task, developer_plan, reviewer, qa
                )
                if workflow.task_spec is None
                else self._implementer.implement(
                    snapshot, planner_task, developer_plan, reviewer, qa,
                    workflow.task_spec,
                )
            )
        except Exception:
            return _result_without_workspace(
                task,
                status="implementation_failed",
                reason="Implementation Agent failed; no workspace changes were applied.",
                base_repository_state=base_repository_state,
            )
        if changeset.task_title != planner_task.title:
            return _result_without_workspace(
                task,
                status="policy_rejected",
                reason=(
                    "Implementation Agent changed the approved task identity."
                ),
                base_repository_state=base_repository_state,
            )

        results: list[SandboxCommandResult] = []
        applied: list[AppliedChange] = []
        prepared: PreparedWorkspace | None = None
        final_status: ImplementationStatus = "implementation_failed"
        stop_reason: str | None = None
        try:
            with TemporaryImplementationWorkspace(repository) as prepared_workspace:
                prepared = prepared_workspace
                policy = self._change_policy.validate(
                    changeset,
                    workspace=prepared.path,
                    files_likely_affected=developer_plan.files_likely_affected,
                )
                if policy.status != "approved":
                    reason = "; ".join(issue.reason for issue in policy.issues)
                    return _workspace_result(
                        task=task,
                        changeset=changeset,
                        prepared=prepared,
                        status=(
                            "human_required"
                            if policy.status == "human_required"
                            else "policy_rejected"
                        ),
                        reason=reason,
                        base_repository_state=base_repository_state,
                    )

                applied = self._applier.apply(changeset, prepared.path)
                for command in qa.proposed_commands:
                    try:
                        self._command_policy.validate(command)
                    except SandboxPolicyError as exc:
                        return _workspace_result(
                            task=task,
                            changeset=changeset,
                            prepared=prepared,
                            applied=applied,
                            status="policy_rejected",
                            reason=f"QA command was rejected before execution: {exc}",
                            base_repository_state=base_repository_state,
                        )
                    result = self._sandbox.run_prepared_workspace(
                        prepared, validated_image, command
                    )
                    results.append(result)
                    if result.timed_out or result.exit_code != 0:
                        final_status = "verification_failed"
                        stop_reason = (
                            "Verification timed out."
                            if result.timed_out
                            else "A verification command failed."
                        )
                        break
                else:
                    final_status = "verified"
        except ChangeApplicationError:
            stop_reason = "The ChangeSet could not be applied safely."
        except SandboxError:
            stop_reason = "Sandbox verification failed safely."
        except Exception:
            stop_reason = "Controlled implementation failed safely."

        summary = None
        if prepared is not None:
            summary = WorkspaceSummary(
                initial_file_count=prepared.file_count,
                initial_total_bytes=prepared.total_bytes,
                changes_applied=len(applied),
                cleaned_up=True,
            )
        return ImplementationResult(
            task=task,
            base_repository_state=base_repository_state,
            changeset=changeset,
            verified_changeset_sha256=(
                changeset_sha256(changeset) if final_status == "verified" else None
            ),
            applied_changes=applied,
            verification_results=results,
            status=final_status,
            stop_reason=stop_reason,
            workspace_summary=summary,
            tests_passed=final_status == "verified",
            commands_executed_count=len(results),
        )


def _approved_contracts(
    workflow: WorkflowResult,
) -> tuple[PlannerTask, DeveloperPlan, ReviewerDecision, QAPlan] | None:
    task = workflow.planner.selected_task if workflow.planner is not None else None
    if (
        workflow.status != "ready_for_implementation"
        or task is None
        or workflow.developer is None
        or workflow.reviewer is None
        or workflow.qa is None
        or workflow.reviewer.decision != "approved"
        or workflow.qa.readiness != "ready"
    ):
        return None
    return task, workflow.developer, workflow.reviewer, workflow.qa


def _task_title(workflow: WorkflowResult) -> str:
    if workflow.planner is not None and workflow.planner.selected_task is not None:
        return workflow.planner.selected_task.title
    return "Unavailable workflow task"


def _result_without_workspace(
    task: str,
    *,
    status: ImplementationStatus,
    reason: str,
    base_repository_state: BaseRepositoryState | None = None,
) -> ImplementationResult:
    return ImplementationResult(
        task=task,
        base_repository_state=base_repository_state,
        status=status,
        stop_reason=reason,
        tests_passed=False,
        commands_executed_count=0,
    )


def _workspace_result(
    *,
    task: str,
    changeset: ChangeSet,
    prepared: PreparedWorkspace,
    status: ImplementationStatus,
    reason: str,
    applied: list[AppliedChange] | None = None,
    base_repository_state: BaseRepositoryState | None = None,
) -> ImplementationResult:
    applied_changes = applied or []
    return ImplementationResult(
        task=task,
        base_repository_state=base_repository_state,
        changeset=changeset,
        applied_changes=applied_changes,
        status=status,
        stop_reason=reason,
        workspace_summary=WorkspaceSummary(
            initial_file_count=prepared.file_count,
            initial_total_bytes=prepared.total_bytes,
            changes_applied=len(applied_changes),
            cleaned_up=True,
        ),
        tests_passed=False,
        commands_executed_count=0,
    )


def run_controlled_implementation(
    repository: Path,
    snapshot: RepositorySnapshot,
    workflow: WorkflowResult,
    image: str,
    base_repository_state: BaseRepositoryState | None = None,
) -> ImplementationResult:
    """CLI boundary for one default controlled implementation pass."""
    return ControlledImplementationService().run(
        repository=repository,
        snapshot=snapshot,
        workflow=workflow,
        image=image,
        base_repository_state=base_repository_state,
    )
