"""Tool-free QA Agent for read-only verification planning."""

from __future__ import annotations

from agents import Agent, Runner

from fanatic_agents.agents._shared import (
    SynchronousRunner,
    resolve_model,
    run_structured_agent,
    untrusted_task_context,
)
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.intake.models import TaskSpec
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerTask,
    QAPlan,
    ReviewerDecision,
)

QA_INSTRUCTIONS = """
You are the read-only QA Agent for Fanatic Agents Sprint 3 and have no tools. Prepare a
bounded verification plan only after reviewer approval. You cannot run tests, Docker,
or commands. Proposed commands must be SandboxCommand argv arrays and must not use
shells, inline code, operators, Docker, SSH, secrets, or destructive operations. State
expected signals, risks, and whether the proposal is ready or needs human attention.
""".strip()


class QAAgentService:
    """Run one structured QA call without tools."""

    def __init__(
        self, *, runner: SynchronousRunner = Runner, model: str | None = None
    ) -> None:
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents QA",
            instructions=QA_INSTRUCTIONS,
            model=resolve_model(model),
            output_type=QAPlan,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        return self._agent

    def plan(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        developer_plan: DeveloperPlan,
        reviewer: ReviewerDecision,
        task_spec: TaskSpec | None = None,
    ) -> QAPlan:
        prompt = (
            "Create a read-only verification plan from this approved context.\n\n"
            "Repository snapshot:\n"
            + snapshot.model_dump_json(indent=2)
            + "\n\nSelected task:\n"
            + task.model_dump_json(indent=2)
            + "\n\nDeveloper plan:\n"
            + developer_plan.model_dump_json(indent=2)
            + "\n\nReviewer decision:\n"
            + reviewer.model_dump_json(indent=2)
            + untrusted_task_context(task_spec)
        )
        return run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=QAPlan,
            role="QA Agent",
        )
