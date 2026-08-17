"""CLI gates and rendering for explicit Sprint 6 delivery."""

from __future__ import annotations

from pathlib import Path

import yaml
from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.core.settings import ApplicationSettings
from fanatic_agents.delivery.models import DeliveryResult
from fanatic_agents.github.client import GitHubPreflight

runner = CliRunner()


def delivered(worktree: Path) -> DeliveryResult:
    return DeliveryResult(
        repository="/source/project",
        worktree_path=str(worktree),
        base_branch="base",
        base_commit="a" * 40,
        branch="fanatic/task",
        commit_sha="b" * 40,
        remote="origin",
        remote_branch="fanatic/task",
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        status="delivered",
    )


def test_deliver_is_separate_deterministic_command(monkeypatch, tmp_path: Path) -> None:
    worktree = tmp_path / "promotion"
    calls: list[dict[str, object]] = []

    def fake_delivery(path: Path, **kwargs):
        calls.append({"path": path, **kwargs})
        return delivered(path)

    monkeypatch.setattr("fanatic_agents.cli.main.deliver_promotion", fake_delivery)
    monkeypatch.setattr(
        "fanatic_agents.cli.main.configure_openai_sdk",
        lambda *_args: (_ for _ in ()).throw(AssertionError("delivery must not use OpenAI")),
    )
    result = runner.invoke(
        app,
        [
            "workflow", "deliver", str(worktree),
            "--commit-message", "fanatic: exact task",
            "--pr-title", "Exact task",
        ],
    )
    assert result.exit_code == 0 and len(calls) == 1
    assert calls[0]["commit_message"] == "fanatic: exact task"
    for expected in (
        "Fanatic Agents Git Delivery", "VERIFIED / PROMOTED", "fanatic/task",
        "APPROVED", "origin/fanatic/task", "#42", "DISABLED",
        "DELIVERED_FOR_REVIEW", "original working tree was not modified",
    ):
        assert expected in result.stdout


def test_delivery_check_renders_no_side_effect_mode(monkeypatch, tmp_path: Path) -> None:
    def fake_delivery(path: Path, **kwargs):
        assert kwargs["check_only"] is True
        return DeliveryResult(
            repository="/source/project",
            worktree_path=str(path),
            base_branch="base",
            base_commit="a" * 40,
            branch="fanatic/task",
            status="ready",
            stop_reason="Delivery preflight passed; no changes were made.",
        )

    monkeypatch.setattr("fanatic_agents.cli.main.deliver_promotion", fake_delivery)
    result = runner.invoke(app, ["workflow", "deliver", str(tmp_path), "--check"])
    assert result.exit_code == 0
    assert "CHECK ONLY - NO SIDE EFFECTS" in result.stdout
    assert "NOT CREATED" in result.stdout and "NOT PERFORMED" in result.stdout


def test_delivery_config_passes_explicit_permissions_and_repository(
    monkeypatch, tmp_path: Path, valid_config_data: dict[str, object]
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    repository_data = valid_config_data["repository"]
    assert isinstance(repository_data, dict)
    repository_data["path"] = str(repository)
    config = tmp_path / "project.yaml"
    config.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    def fake_delivery(path: Path, **kwargs):
        permissions = kwargs["permissions"]
        assert permissions.commit and permissions.push_branch
        assert permissions.create_pull_request
        assert kwargs["configured_repository"] == repository
        return delivered(path)

    monkeypatch.setattr("fanatic_agents.cli.main.deliver_promotion", fake_delivery)
    result = runner.invoke(
        app, ["workflow", "deliver", str(tmp_path / "promotion"), "--config", str(config)]
    )
    assert result.exit_code == 0


def test_delivery_failure_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        "fanatic_agents.cli.main.deliver_promotion",
        lambda path, **_kwargs: DeliveryResult(
            repository="/source/project",
            worktree_path=str(path),
            status="github_cli_unavailable",
            stop_reason="GitHub CLI is required for delivery.",
        ),
    )
    result = runner.invoke(app, ["workflow", "deliver", str(tmp_path)])
    assert result.exit_code == 1
    assert "GITHUB_CLI_UNAVAILABLE" in result.stdout
    assert "GitHub CLI is required" in result.stdout


def test_doctor_distinguishes_gh_authentication(monkeypatch) -> None:
    monkeypatch.setattr("fanatic_agents.cli.main.shutil.which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(
        "fanatic_agents.cli.main.check_github_cli",
        lambda: GitHubPreflight("not_authenticated", "/usr/bin/gh"),
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.get_settings",
        lambda: ApplicationSettings(_env_file=None),
    )
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0
    assert "FOUND BUT NOT AUTHENTICATED" in result.stdout
