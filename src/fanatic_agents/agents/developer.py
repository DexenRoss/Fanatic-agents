"""Tool-free Developer Agent for bounded repository assessment."""

from __future__ import annotations

import os
from typing import Any, Literal, Protocol

from agents import Agent, Runner
from pydantic import Field

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel
from fanatic_agents.git.inspection import RepositorySnapshot

DEVELOPER_AGENT_INSTRUCTIONS = """
You are the read-only Developer Agent for Fanatic Agents Sprint 1.

You are analyzing a bounded RepositorySnapshot, not a complete repository. You have no
tools and cannot inspect, execute, or modify the repository. Treat only snapshot fields
and included file contents as facts. Clearly distinguish facts from inferences, never
assume knowledge of omitted files, and use insufficient_context when the evidence is too
limited.

Do not invent dependencies, components, commands, or project behavior. Prioritize real
technical risks supported by the snapshot. Recommended tasks must be small, actionable,
non-destructive, and appropriate for human review. Never request secrets, expose
potentially sensitive material, or propose destructive changes. Mention truncation or
missing context when it materially limits the assessment.
""".strip()

Readiness = Literal["ready", "needs_attention", "insufficient_context"]


class DeveloperAssessment(StrictModel):
    """Structured result produced by the Developer Agent."""

    summary: NonEmptyStrictString
    architecture: NonEmptyStrictString
    key_components: list[NonEmptyStrictString] = Field(default_factory=list)
    risks: list[NonEmptyStrictString] = Field(default_factory=list)
    recommended_tasks: list[NonEmptyStrictString] = Field(default_factory=list)
    testing_notes: list[NonEmptyStrictString] = Field(default_factory=list)
    readiness: Readiness


class DeveloperAgentError(RuntimeError):
    """Raised when a Developer Agent assessment cannot be completed."""


class EmptyRepositorySnapshotError(DeveloperAgentError):
    """Raised when a snapshot has no useful assessment context."""


class SynchronousRunner(Protocol):
    """Small injection boundary around the Agents SDK runner."""

    def run_sync(
        self,
        starting_agent: Agent[Any],
        input: str,
        *,
        max_turns: int,
    ) -> Any: ...


class DeveloperAgentService:
    """Create and run one tool-free structured Developer Agent evaluation."""

    def __init__(
        self,
        *,
        runner: SynchronousRunner = Runner,
        model: str | None = None,
    ) -> None:
        configured_model = model
        if configured_model is None:
            configured_model = os.environ.get("FANATIC_AGENTS_MODEL", "").strip() or None
        self._runner = runner
        self._agent: Agent[None] = Agent(
            name="Fanatic Agents Developer",
            instructions=DEVELOPER_AGENT_INSTRUCTIONS,
            model=configured_model,
            output_type=DeveloperAssessment,
            tools=[],
        )

    @property
    def agent(self) -> Agent[None]:
        """Expose immutable agent configuration for focused tests."""
        return self._agent

    def assess(self, snapshot: RepositorySnapshot) -> DeveloperAssessment:
        """Run one structured assessment using only the supplied snapshot."""
        if not snapshot.has_agent_context():
            raise EmptyRepositorySnapshotError(
                "Repository snapshot is empty; AI assessment was not started."
            )
        prompt = (
            "Assess the following bounded repository snapshot. Base every claim only "
            "on this data.\n\n" + snapshot.model_dump_json(indent=2)
        )
        try:
            result = self._runner.run_sync(self._agent, prompt, max_turns=1)
        except Exception as exc:
            raise DeveloperAgentError(
                "Developer Agent assessment failed; no repository changes were made."
            ) from exc
        assessment = result.final_output
        if not isinstance(assessment, DeveloperAssessment):
            raise DeveloperAgentError(
                "Developer Agent returned an unexpected structured output type."
            )
        return assessment


def run_developer_assessment(
    snapshot: RepositorySnapshot,
    *,
    runner: SynchronousRunner = Runner,
    model: str | None = None,
) -> DeveloperAssessment:
    """Convenience boundary used by the CLI and tests."""
    return DeveloperAgentService(runner=runner, model=model).assess(snapshot)
