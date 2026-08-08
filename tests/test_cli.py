"""Tests for the Fanatic Agents CLI."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from fanatic_agents.cli.main import app


runner = CliRunner()


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "config" in result.stdout


def test_doctor_handles_missing_dependencies(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("fanatic_agents.cli.main.shutil.which", lambda _: None)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert "Docker" in result.stdout
    assert "GitHub CLI" in result.stdout
    assert "NOT FOUND" in result.stdout
    assert "NOT CONFIGURED" in result.stdout
    assert "PARTIALLY READY" in result.stdout


def test_doctor_never_prints_api_key(monkeypatch) -> None:
    secret = "super-secret-test-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code == 0
    assert secret not in result.stdout
    assert "OpenAI API Key" in result.stdout


def test_config_validate_accepts_valid_yaml(project_root: Path) -> None:
    config_path = project_root / "projects" / "example.yaml"

    result = runner.invoke(app, ["config", "validate", str(config_path)])

    assert result.exit_code == 0
    assert "Configuration is valid" in result.stdout
    assert "example-project" in result.stdout


def test_config_validate_rejects_invalid_yaml(
    tmp_path: Path, valid_config_data: dict[str, object]
) -> None:
    limits = valid_config_data["limits"]
    assert isinstance(limits, dict)
    limits["max_tasks_per_day"] = -10
    config_path = tmp_path / "invalid.yaml"
    config_path.write_text(yaml.safe_dump(valid_config_data), encoding="utf-8")

    result = runner.invoke(app, ["config", "validate", str(config_path)])

    assert result.exit_code == 1
    assert "Configuration is invalid" in result.stdout
    assert "limits.max_tasks_per_day" in result.stdout

