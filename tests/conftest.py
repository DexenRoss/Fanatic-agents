"""Shared pytest fixtures."""

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def no_color_cli_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep Rich/Typer CLI assertions deterministic across terminals and CI."""
    monkeypatch.setenv("NO_COLOR", "1")


@pytest.fixture()
def valid_config_data() -> dict[str, object]:
    return {
        "project": {"name": "example-project"},
        "repository": {"path": "/workspace/example", "main_branch": "main"},
        "commands": {
            "setup": ['echo "setup"'],
            "test": ['echo "tests"'],
            "build": ['echo "build"'],
        },
        "limits": {
            "max_tasks_per_day": 5,
            "max_runtime_minutes": 90,
            "max_daily_cost_usd": 5.0,
            "max_iterations_per_task": 6,
        },
        "permissions": {
            "read_repository": True,
            "create_branch": True,
            "modify_files": True,
            "run_commands": True,
            "commit": True,
            "push_branch": True,
            "create_pull_request": True,
            "observe_pull_request": True,
            "read_issues": True,
            "merge": False,
            "production_deploy": False,
            "modify_secrets": False,
            "destructive_database_changes": False,
        },
        "intake": {
            "enabled": False,
            "source": "github_issues",
            "required_labels": ["fanatic:ready"],
            "blocked_labels": ["fanatic:blocked", "fanatic:manual"],
            "max_candidates": 50,
            "ordering": "priority_then_oldest",
        },
    }


@pytest.fixture()
def project_root() -> Path:
    return Path(__file__).resolve().parents[1]
