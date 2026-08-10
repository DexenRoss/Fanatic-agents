"""Explicit, single-pass, read-only multi-agent orchestration."""

from __future__ import annotations

from typing import Protocol

from fanatic_agents.agents.developer_planning import DeveloperPlanningAgentService
from fanatic_agents.agents.planner import PlannerAgentService
from fanatic_agents.agents.qa import QAAgentService
from fanatic_agents.agents.reviewer import ReviewerAgentService
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.orchestrator.models import (
    CommandValidation,
    DeveloperPlan,
    PlannerOutput,
    PlannerTask,
    QAPlan,
    RepositorySnapshotMetadata,
    ReviewerDecision,
    WorkflowResult,
)
from fanatic_agents.sandbox.errors import SandboxPolicyError
from fanatic_agents.sandbox.models import SandboxCommand
from fanatic_agents.sandbox.policy import CommandPolicy


class Planner(Protocol):
    def plan(self, snapshot: RepositorySnapshot) -> PlannerOutput: ...


class DeveloperPlanner(Protocol):
    def plan(
        self, snapshot: RepositorySnapshot, task: PlannerTask
    ) -> DeveloperPlan: ...


class Reviewer(Protocol):
    def review(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        plan: DeveloperPlan,
    ) -> ReviewerDecision: ...


class QualityPlanner(Protocol):
    def plan(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        developer_plan: DeveloperPlan,
        reviewer: ReviewerDecision,
    ) -> QAPlan: ...


class WorkflowOrchestrator:
    """Own agent order, context, policy gates, and all stop conditions."""

    def __init__(
        self,
        *,
        planner: Planner | None = None,
        developer: DeveloperPlanner | None = None,
        reviewer: Reviewer | None = None,
        qa: QualityPlanner | None = None,
        command_policy: CommandPolicy | None = None,
    ) -> None:
        self._planner = planner or PlannerAgentService()
        self._developer = developer or DeveloperPlanningAgentService()
        self._reviewer = reviewer or ReviewerAgentService()
        self._qa = qa or QAAgentService()
        self._command_policy = command_policy or CommandPolicy()

    def run(self, snapshot: RepositorySnapshot) -> WorkflowResult:
        """Run at most one call per role and never execute a proposed command."""
        repository = RepositorySnapshotMetadata.from_snapshot(snapshot)
        if not snapshot.has_agent_context():
            return WorkflowResult(
                repository=repository,
                status="insufficient_context",
                stop_reason="The repository snapshot has insufficient agent context.",
            )

        try:
            planner_output = self._planner.plan(snapshot)
        except Exception:
            return WorkflowResult(
                repository=repository,
                status="failed",
                stop_reason="Planner Agent failed; later agents were not called.",
            )

        if planner_output.status == "insufficient_context":
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                status="insufficient_context",
                stop_reason="Planner reported insufficient repository context.",
            )

        task = planner_output.selected_task
        if task is None:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                status="failed",
                stop_reason="Planner returned no selected task.",
            )
        if task.requires_human_approval:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                status="human_required",
                stop_reason="Planner task requires human approval.",
            )
        if task.risk_level == "high":
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                status="human_required",
                stop_reason="High-risk Planner task requires human approval.",
            )

        try:
            developer_plan = self._developer.plan(snapshot, task)
        except Exception:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                status="failed",
                stop_reason="Developer Planning Agent failed; later agents were not called.",
            )

        if developer_plan.requires_human_approval:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                status="human_required",
                stop_reason="Developer plan requires human approval.",
            )

        developer_validations = self._validate_commands(
            developer_plan.proposed_commands
        )
        if any(not validation.valid for validation in developer_validations):
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                developer_command_validations=developer_validations,
                status="changes_requested",
                stop_reason=(
                    "Developer proposed a command rejected by CommandPolicy; "
                    "no command was executed."
                ),
            )

        try:
            reviewer_decision = self._reviewer.review(
                snapshot, task, developer_plan
            )
        except Exception:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                developer_command_validations=developer_validations,
                status="failed",
                stop_reason="Reviewer Agent failed; later agents were not called.",
            )

        if reviewer_decision.decision == "changes_requested":
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                developer_command_validations=developer_validations,
                reviewer=reviewer_decision,
                status="changes_requested",
                stop_reason="Reviewer requested changes; QA was not called.",
            )
        if reviewer_decision.decision == "human_required":
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                developer_command_validations=developer_validations,
                reviewer=reviewer_decision,
                status="human_required",
                stop_reason="Reviewer requires a human decision; QA was not called.",
            )

        try:
            qa_plan = self._qa.plan(
                snapshot, task, developer_plan, reviewer_decision
            )
        except Exception:
            return WorkflowResult(
                repository=repository,
                planner=planner_output,
                developer=developer_plan,
                developer_command_validations=developer_validations,
                reviewer=reviewer_decision,
                status="failed",
                stop_reason="QA Agent failed; the workflow stopped safely.",
            )

        qa_validations = self._validate_commands(qa_plan.proposed_commands)
        common = {
            "repository": repository,
            "planner": planner_output,
            "developer": developer_plan,
            "developer_command_validations": developer_validations,
            "reviewer": reviewer_decision,
            "qa": qa_plan,
            "qa_command_validations": qa_validations,
        }
        if qa_plan.readiness == "human_required":
            return WorkflowResult(
                **common,
                status="human_required",
                stop_reason="QA plan requires human attention.",
            )
        if any(not validation.valid for validation in qa_validations):
            return WorkflowResult(
                **common,
                status="changes_requested",
                stop_reason=(
                    "QA proposed a command rejected by CommandPolicy; "
                    "no command was executed."
                ),
            )
        if qa_plan.readiness == "needs_attention":
            return WorkflowResult(
                **common,
                status="changes_requested",
                stop_reason="QA plan needs attention before implementation.",
            )
        return WorkflowResult(**common, status="ready_for_implementation")

    def _validate_commands(
        self, commands: list[SandboxCommand]
    ) -> list[CommandValidation]:
        results: list[CommandValidation] = []
        for command in commands:
            try:
                self._command_policy.validate(command)
            except SandboxPolicyError as exc:
                results.append(
                    CommandValidation(
                        command=command,
                        valid=False,
                        rejection_reason=str(exc),
                    )
                )
            else:
                results.append(CommandValidation(command=command, valid=True))
        return results


def run_workflow(snapshot: RepositorySnapshot) -> WorkflowResult:
    """CLI boundary for one default read-only workflow pass."""
    return WorkflowOrchestrator().run(snapshot)
