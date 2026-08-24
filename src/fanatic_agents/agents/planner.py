"""Tool-free Planner Agent for selecting one bounded next task."""

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

    def plan(
        self, snapshot: RepositorySnapshot, task_spec: TaskSpec | None = None
    ) -> PlannerOutput:
        direction = (
            (
                "Plan exactly the supplied selected task; do not invent or substitute "
                "a task. Copy TaskSpec.task_id exactly and verbatim into "
                f"PlannerOutput.source_task_id: {task_spec.task_id!r}."
            )
            if task_spec is not None
            else "Select one next task from this bounded snapshot."
        )
        prompt = (
            direction + " Treat only supplied data as fact.\n\nRepository snapshot:\n"
            + snapshot.model_dump_json(indent=2)
            + untrusted_task_context(task_spec)
        )
        result = run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=PlannerOutput,
            role="Planner Agent",
        )
        return result
