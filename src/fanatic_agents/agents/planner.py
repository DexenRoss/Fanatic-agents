"""Tool-free Planner Agent for selecting one bounded next task."""

from __future__ import annotations

from agents import Agent, Runner

from fanatic_agents.agents._shared import (
    SynchronousRunner,
    resolve_model,
    run_structured_agent,
)
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.orchestrator.models import PlannerOutput

PLANNER_INSTRUCTIONS = """
You are the read-only Planner Agent for Fanatic Agents Sprint 3. You receive only a
bounded RepositorySnapshot and have no tools. Select exactly one small, actionable,
verifiable next task supported by snapshot facts. Distinguish facts from assumptions
and do not invent requirements. If context is insufficient, return insufficient_context
without a task. Mark high-risk, destructive, production, secret, sensitive-authentication,
significant-data-deletion, or major architectural decisions for human approval.
""".strip()


class PlannerAgentService:
    """Run one structured Planner call without tools."""

    def __init__(
        self, *, runner: SynchronousRunner = Runner, model: str | None = None
    ) -> None:
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents Planner",
            instructions=PLANNER_INSTRUCTIONS,
            model=resolve_model(model),
            output_type=PlannerOutput,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        return self._agent

    def plan(self, snapshot: RepositorySnapshot) -> PlannerOutput:
        prompt = (
            "Select one next task from this bounded snapshot. Treat only supplied data "
            "as fact.\n\n" + snapshot.model_dump_json(indent=2)
        )
        return run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=PlannerOutput,
            role="Planner Agent",
        )
