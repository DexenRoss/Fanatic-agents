"""Tests for structured, tool-free Developer Agent execution."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from fanatic_agents.agents.developer import (
    DeveloperAgentError,
    DeveloperAgentService,
    DeveloperAssessment,
    EmptyRepositorySnapshotError,
)
from fanatic_agents.git.inspection import (
    RepositorySnapshot,
    SnapshotFile,
    SnapshotTruncation,
)


def _snapshot(*, with_context: bool = True) -> RepositorySnapshot:
    files = [SnapshotFile(path="README.md", content="# Sample")] if with_context else []
    paths = ["README.md"] if with_context else []
    return RepositorySnapshot(
        repository_name="sample",
        is_git_repository=False,
        detected_technologies=["Python"] if with_context else [],
        important_files=paths,
        relevant_paths=paths,
        files=files,
        truncation=SnapshotTruncation(
            max_relevant_files=10,
            max_content_files=5,
            max_source_content_files=2,
            max_characters_per_file=100,
            max_total_characters=500,
            files_considered=len(paths),
            relevant_files_included=len(paths),
            relevant_files_omitted=0,
            content_files_included=len(files),
            content_files_omitted=0,
            truncated_files=0,
            total_characters=sum(len(file.content) for file in files),
        ),
    )


def _assessment() -> DeveloperAssessment:
    return DeveloperAssessment(
        summary="A small Python project.",
        architecture="The snapshot shows one documented component.",
        key_components=["README"],
        risks=["The snapshot is intentionally bounded."],
        recommended_tasks=["Review the documented test command."],
        testing_notes=["No commands were executed."],
        readiness="needs_attention",
    )


class FakeRunner:
    def __init__(self, assessment: DeveloperAssessment) -> None:
        self.assessment = assessment
        self.calls: list[tuple[Any, str, int]] = []

    def run_sync(
        self, starting_agent: Any, input: str, *, max_turns: int
    ) -> SimpleNamespace:
        self.calls.append((starting_agent, input, max_turns))
        return SimpleNamespace(final_output=self.assessment)


def test_developer_assessment_validates_structured_output() -> None:
    assessment = _assessment()

    assert assessment.readiness == "needs_attention"
    with pytest.raises(ValidationError):
        DeveloperAssessment(
            summary="Summary",
            architecture="Architecture",
            readiness="unknown",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        DeveloperAssessment(summary="", architecture="Architecture", readiness="ready")


def test_service_uses_tool_free_agent_and_one_runner_turn() -> None:
    runner = FakeRunner(_assessment())
    service = DeveloperAgentService(runner=runner)

    assessment = service.assess(_snapshot())

    assert assessment == _assessment()
    assert service.agent.tools == []
    assert service.agent.output_type is DeveloperAssessment
    assert len(runner.calls) == 1
    agent, prompt, max_turns = runner.calls[0]
    assert agent is service.agent
    assert "RepositorySnapshot" not in prompt
    assert '"repository_name": "sample"' in prompt
    assert max_turns == 1


def test_model_can_be_configured_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FANATIC_AGENTS_MODEL", "test-model")

    service = DeveloperAgentService(runner=FakeRunner(_assessment()))

    assert service.agent.model == "test-model"


def test_missing_model_environment_uses_sdk_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FANATIC_AGENTS_MODEL", raising=False)

    service = DeveloperAgentService(runner=FakeRunner(_assessment()))

    assert service.agent.model is None


def test_empty_snapshot_never_calls_runner() -> None:
    runner = FakeRunner(_assessment())
    service = DeveloperAgentService(runner=runner)

    with pytest.raises(EmptyRepositorySnapshotError, match="empty"):
        service.assess(_snapshot(with_context=False))

    assert runner.calls == []


def test_runner_failures_are_wrapped_without_sensitive_details() -> None:
    class FailingRunner:
        def run_sync(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("sk-test-secret")

    service = DeveloperAgentService(runner=FailingRunner())

    with pytest.raises(DeveloperAgentError) as error:
        service.assess(_snapshot())

    assert "sk-test-secret" not in str(error.value)


def test_unexpected_output_type_is_rejected() -> None:
    runner = FakeRunner(_assessment())
    runner.assessment = "free-form JSON"  # type: ignore[assignment]

    with pytest.raises(DeveloperAgentError, match="unexpected structured output"):
        DeveloperAgentService(runner=runner).assess(_snapshot())
