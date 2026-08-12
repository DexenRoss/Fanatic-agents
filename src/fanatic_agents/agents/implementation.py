"""Tool-free Implementation Agent producing a bounded structured ChangeSet."""

from __future__ import annotations

from agents import Agent, Runner

from fanatic_agents.agents._shared import SynchronousRunner, resolve_model, run_structured_agent
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.implementation.models import ChangeSet
from fanatic_agents.orchestrator.models import DeveloperPlan, PlannerTask, QAPlan, ReviewerDecision

IMPLEMENTATION_INSTRUCTIONS = """
You are the tool-free Implementation Agent for Fanatic Agents Sprint 4. Produce exactly
one bounded ChangeSet using complete file contents for create and modify operations.
You have no filesystem, shell, Git, Docker, or network tools. Use only the supplied,
bounded RepositorySnapshot and approved PlannerTask, DeveloperPlan, ReviewerDecision,
and QAPlan. Stay aligned with files_likely_affected. Never target secrets, .env, .git,
AGENTS.md, CI workflows, deployment, credentials, symlinks, or paths outside the plan.
Do not emit commands, patches, diffs, scripts for applying changes, or retries.
""".strip()


class ImplementationAgentService:
    """Run at most one structured Implementation Agent call without tools."""

    def __init__(
        self, *, runner: SynchronousRunner = Runner, model: str | None = None
    ) -> None:
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents Implementation",
            instructions=IMPLEMENTATION_INSTRUCTIONS,
            model=resolve_model(model),
            output_type=ChangeSet,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        return self._agent

    def implement(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        developer_plan: DeveloperPlan,
        reviewer: ReviewerDecision,
        qa: QAPlan,
    ) -> ChangeSet:
        context = {
            "repository_snapshot": snapshot.model_dump(mode="json"),
            "planner_task": task.model_dump(mode="json"),
            "developer_plan": developer_plan.model_dump(mode="json"),
            "reviewer_decision": reviewer.model_dump(mode="json"),
            "qa_plan": qa.model_dump(mode="json"),
        }
        import json

        prompt = "Produce one ChangeSet from this approved bounded context.\n\n" + json.dumps(
            context, indent=2
        )
        return run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=ChangeSet,
            role="Implementation Agent",
        )
