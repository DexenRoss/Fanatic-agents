"""Tool-free Reviewer Agent for one-pass plan review."""

from __future__ import annotations

from agents import Agent, Runner

from fanatic_agents.agents._shared import (
    SynchronousRunner,
    resolve_model,
    run_structured_agent,
)
from fanatic_agents.git.inspection import RepositorySnapshot
from fanatic_agents.orchestrator.models import (
    DeveloperPlan,
    PlannerTask,
    ReviewerDecision,
)

REVIEWER_INSTRUCTIONS = """
You are the read-only Reviewer Agent for Fanatic Agents Sprint 3 and have no tools.
Review one DeveloperPlan against its PlannerTask and bounded RepositorySnapshot. Check
for unsupported assumptions, excessive scope, secrets, destructive operations,
inappropriate commands, missing tests, and unjustified architectural changes. Approve,
request changes, or require a human. Do not implement changes or create an iteration.
""".strip()


class ReviewerAgentService:
    """Run one structured Reviewer call without tools."""

    def __init__(
        self, *, runner: SynchronousRunner = Runner, model: str | None = None
    ) -> None:
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents Reviewer",
            instructions=REVIEWER_INSTRUCTIONS,
            model=resolve_model(model),
            output_type=ReviewerDecision,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        return self._agent

    def review(
        self,
        snapshot: RepositorySnapshot,
        task: PlannerTask,
        plan: DeveloperPlan,
    ) -> ReviewerDecision:
        prompt = (
            "Review this bounded, read-only proposal.\n\nRepository snapshot:\n"
            + snapshot.model_dump_json(indent=2)
            + "\n\nSelected task:\n"
            + task.model_dump_json(indent=2)
            + "\n\nDeveloper plan:\n"
            + plan.model_dump_json(indent=2)
        )
        return run_structured_agent(
            runner=self._runner,
            agent=self._agent,
            prompt=prompt,
            output_type=ReviewerDecision,
            role="Reviewer Agent",
        )
