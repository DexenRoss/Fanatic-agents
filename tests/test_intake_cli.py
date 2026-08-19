"""CLI boundaries and output for read-only task intake."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.intake.models import (
    GitHubIssueCandidate,
    TaskDiscoveryResult,
    TaskIntakeResult,
    TaskSpec,
)

runner = CliRunner()


def test_task_help_tree_is_exposed() -> None:
    for arguments in (
        ["--help"],
        ["task", "--help"],
        ["task", "discover", "--help"],
        ["task", "select", "--help"],
    ):
        result = runner.invoke(app, arguments)
        assert result.exit_code == 0
    assert "task" in runner.invoke(app, ["--help"]).stdout


def test_discover_renders_eligible_issues_and_no_mutation(monkeypatch) -> None:
    candidate = GitHubIssueCandidate(
        repository="owner/repo",
        number=42,
        title="Fix calculator",
        body="description",
        url="https://github.com/owner/repo/issues/42",
        state="open",
        labels=["bug", "fanatic:ready"],
        created_at=datetime(2025, 1, 1, tzinfo=UTC),
        updated_at=datetime(2025, 1, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.discover_tasks",
        lambda *_args, **_kwargs: TaskDiscoveryResult(
            repository="/repo",
            github_repository="owner/repo",
            candidates_fetched=3,
            candidates_eligible=1,
            eligible_candidates=[candidate],
            status="tasks_discovered",
        ),
    )

    result = runner.invoke(app, ["task", "discover", "/repo"])

    assert result.exit_code == 0
    for text in (
        "Fanatic Agents Task Discovery",
        "owner/repo",
        "#42",
        "fanatic:ready",
        "Blocked / ignored",
        "No task was selected",
        "GitHub was not modified",
    ):
        assert text in result.stdout


def test_select_renders_stop_boundary_and_untrusted_source(monkeypatch) -> None:
    selected_at = datetime(2025, 1, 1, tzinfo=UTC)
    task = TaskSpec(
        task_id="github:owner/repo#42",
        repository="/repo",
        issue_number=42,
        issue_url="https://github.com/owner/repo/issues/42",
        title="Fix calculator",
        description="description",
        labels=["fanatic:ready", "priority:p1"],
        priority="p1",
        base_branch="main",
        base_commit_sha="a" * 40,
        selected_at=selected_at,
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.select_task",
        lambda *_args, **_kwargs: TaskIntakeResult(
            repository="/repo",
            github_repository="owner/repo",
            candidates_fetched=2,
            candidates_eligible=2,
            selected_task=task,
            receipt_path="/external/issue-42.json",
            status="task_selected",
        ),
    )

    result = runner.invoke(app, ["task", "select", "/repo"])

    assert result.exit_code == 0
    for text in (
        "Fanatic Agents Task Intake",
        "#42 Fix calculator",
        "P1",
        "UNTRUSTED",
        "main @ aaaaaaa",
        "GitHub mutation",
        "Git mutation",
        "NONE",
        "Agents called",
        "TASK_SELECTED",
        "NOT started implementation",
    ):
        assert text in result.stdout


def test_empty_backlog_is_successful_terminal_result(monkeypatch) -> None:
    monkeypatch.setattr(
        "fanatic_agents.cli.main.select_task",
        lambda *_args, **_kwargs: TaskIntakeResult(
            repository="/repo",
            github_repository="owner/repo",
            status="no_eligible_tasks",
            stop_reason="No eligible GitHub Issues were found.",
        ),
    )

    result = runner.invoke(app, ["task", "select", "/repo"])

    assert result.exit_code == 0
    assert "NO_ELIGIBLE_TASKS" in result.stdout
    assert "Selected" in result.stdout and "NONE" in result.stdout
