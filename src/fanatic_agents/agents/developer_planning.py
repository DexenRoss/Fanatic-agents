"""Tool-free Developer Planning Agent; does not replace Sprint 1 assessment."""

from __future__ import annotations

from agents import Agent, Runner

from fanatic_agents.agents._shared import (
    SynchronousRunner,
    resolve_model,
    run_structured_agent,
)
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.orchestrator.models import DeveloperPlan, PlannerTask

DEVELOPER_PLANNING_INSTRUCTIONS = """
You are the read-only Developer Planning Agent for Fanatic Agents Sprint 3. Produce a
small implementation plan for the single supplied PlannerTask using only the bounded
snapshot. You cannot inspect or modify files and cannot execute commands. Commands are
proposals only and must be SandboxCommand argv arrays, never shell strings. Do not
propose shells, inline code, Docker, SSH, operators, secrets, destructive actions, or
scope beyond the task. State assumptions and require human approval when warranted.
""".strip()


class DeveloperPlanningAgentService:
    """Run one structured Developer Planning call without tools."""

    def __init__(
        self, *, runner: SynchronousRunner = Runner, model: str | None = None
    ) -> None:
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents Developer Planning",
            instructions=DEVELOPER_PLANNING_INSTRUCTIONS,
            model=resolve_model(model),
            output_type=DeveloperPlan,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        return self._agent

    def plan(
        self, snapshot: RepositorySnapshot, task: PlannerTask
    ) -> DeveloperPlan:
        prompt = (
            "Create a read-only implementation proposal from exactly this context.\n\n"
            "Repository snapshot:\n"
            + snapshot.model_dump_json(indent=2)
            + "\n\nSelected task:\n"
            + task.model_dump_json(indent=2)
        )
        return run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=DeveloperPlan,
            role="Developer Planning Agent",
        )
