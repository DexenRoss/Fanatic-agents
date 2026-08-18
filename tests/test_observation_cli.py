"""CLI behavior for deterministic Sprint 7 observation."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import yaml
from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.observation.models import PullRequestCheck, PullRequestObservation

runner = CliRunner()


def observation(worktree: Path, status: str = "ready_for_human_merge") -> PullRequestObservation:
    return PullRequestObservation(
        repository="owner/repo",
        promotion_worktree=str(worktree),
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
        base_branch="main",
        head_branch="fanatic/task",
        expected_head_sha="b" * 40,
        observed_head_sha="b" * 40,
        pr_state="open",
        mergeable="mergeable",
        review_state="approved",
        approvals=1,
        checks=[
            PullRequestCheck(
                name="test",
                context="CI",
                status="completed",
                conclusion="success",
            )
        ],
        ci_state="passed",
        status=status,  # type: ignore[arg-type]
        observed_at=datetime.now(UTC),
    )


def test_observe_is_separate_zero_model_call_read_only_command(
    monkeypatch, tmp_path: Path
) -> None:
    calls: list[dict[str, object]] = []

    def fake_observe(path: Path, **kwargs):
        calls.append({"path": path, **kwargs})
        return observation(path)

    monkeypatch.setattr("fanatic_agents.cli.main.observe_once", fake_observe)
    monkeypatch.setattr(
        "fanatic_agents.cli.main.configure_openai_sdk",
        lambda *_args: (_ for _ in ()).throw(AssertionError("observe must not use OpenAI")),
    )
    result = runner.invoke(app, ["workflow", "observe", str(tmp_path)])
    assert result.exit_code == 0 and len(calls) == 1
    for expected in (
        "Fanatic Agents Pull Request Observation",
        "owner/repo",
        "#42",
        "fanatic/task -> main",
        "VERIFIED",
        "PASSED",
        "CI / test",
        "APPROVED",
        "MERGEABLE",
        "DISABLED",
        "READY_FOR_HUMAN_MERGE",
        "read-only",
    ):
        assert expected in result.stdout


def test_observe_watch_passes_bounded_options(monkeypatch, tmp_path: Path) -> None:
    calls: list[dict[str, object]] = []

    def fake_watch(path: Path, **kwargs):
        calls.append({"path": path, **kwargs})
        return observation(path, "ci_failed")

    monkeypatch.setattr("fanatic_agents.cli.main.observe_until_terminal", fake_watch)
    result = runner.invoke(
        app,
        [
            "workflow",
            "observe",
            str(tmp_path),
            "--watch",
            "--interval-seconds",
            "10",
            "--timeout-seconds",
            "120",
        ],
    )
    assert result.exit_code == 0 and len(calls) == 1
    assert calls[0]["interval_seconds"] == 10.0
    assert calls[0]["timeout_seconds"] == 120.0
    assert "CI_FAILED" in result.stdout


def test_observe_config_passes_explicit_read_permission(
    monkeypatch,
    tmp_path: Path,
    valid_config_data: dict[str, object],
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    repository_data = valid_config_data["repository"]
    assert isinstance(repository_data, dict)
    repository_data["path"] = str(repository)
    config = tmp_path / "project.yaml"
    config.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    def fake_observe(path: Path, **kwargs):
        assert kwargs["permissions"].observe_pull_request is True
        assert kwargs["configured_repository"] == repository
        return observation(path)

    monkeypatch.setattr("fanatic_agents.cli.main.observe_once", fake_observe)
    result = runner.invoke(
        app,
        ["workflow", "observe", str(tmp_path / "promotion"), "--config", str(config)],
    )
    assert result.exit_code == 0


def test_observation_provenance_failure_exits_nonzero(monkeypatch, tmp_path: Path) -> None:
    failed = PullRequestObservation(
        repository=str(tmp_path),
        promotion_worktree=str(tmp_path),
        status="invalid_delivery",
        stop_reason="Invalid receipt.",
        observed_at=datetime.now(UTC),
    )
    monkeypatch.setattr("fanatic_agents.cli.main.observe_once", lambda *_args, **_kwargs: failed)
    result = runner.invoke(app, ["workflow", "observe", str(tmp_path)])
    assert result.exit_code == 1
    assert "INVALID_DELIVERY" in result.stdout
    assert "Invalid receipt" in result.stdout


def test_observe_cli_rejects_unbounded_watch_values(tmp_path: Path) -> None:
    too_fast = runner.invoke(
        app,
        ["workflow", "observe", str(tmp_path), "--interval-seconds", "9"],
    )
    too_long = runner.invoke(
        app,
        ["workflow", "observe", str(tmp_path), "--timeout-seconds", "1801"],
    )
    assert too_fast.exit_code == 2
    assert too_long.exit_code == 2
