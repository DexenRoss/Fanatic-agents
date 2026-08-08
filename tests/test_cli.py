"""Tests for the Fanatic Agents CLI."""

from pathlib import Path

import yaml
from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.agents.developer import DeveloperAssessment


runner = CliRunner()


def test_cli_help() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "doctor" in result.stdout
    assert "config" in result.stdout
    assert "inspect" in result.stdout


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


def test_inspect_without_ai_never_calls_agent(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'sample'\n", encoding="utf-8"
    )
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", unexpected_call
    )

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "Fanatic Agents Repository Inspection" in result.stdout
    assert "Python" in result.stdout
    assert "AI Analysis" in result.stdout
    assert "NOT REQUESTED" in result.stdout


def test_inspect_without_ai_works_without_api_key(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    result = runner.invoke(app, ["inspect", str(tmp_path)])

    assert result.exit_code == 0
    assert "README.md" in result.stdout


def test_inspect_ai_without_api_key_fails_before_agent_call(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", unexpected_call
    )

    result = runner.invoke(app, ["inspect", str(tmp_path), "--ai"])

    assert result.exit_code == 1
    assert "requires OPENAI_API_KEY" in result.stdout
    assert "no API request was made" in result.stdout


def test_inspect_ai_renders_structured_assessment(
    tmp_path: Path, monkeypatch
) -> None:
    (tmp_path / "README.md").write_text("# Local", encoding="utf-8")
    fake_key = "fake-test-key-never-send"
    monkeypatch.setenv("OPENAI_API_KEY", fake_key)
    assessment = DeveloperAssessment(
        summary="A bounded sample.",
        architecture="One documented component.",
        key_components=["CLI"],
        risks=["Limited snapshot"],
        recommended_tasks=["Add a focused test"],
        testing_notes=["No project command was executed"],
        readiness="needs_attention",
    )
    calls = []

    def fake_assessment(snapshot):
        calls.append(snapshot)
        return assessment

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", fake_assessment
    )

    result = runner.invoke(app, ["inspect", str(tmp_path), "--ai"])

    assert result.exit_code == 0
    assert len(calls) == 1
    assert "Developer Agent Assessment" in result.stdout
    assert "A bounded sample." in result.stdout
    assert "needs_attention" in result.stdout
    assert fake_key not in result.stdout


def test_inspect_reports_invalid_path(tmp_path: Path) -> None:
    result = runner.invoke(app, ["inspect", str(tmp_path / "missing")])

    assert result.exit_code == 1
    assert "Repository inspection failed" in result.stdout
    assert "does not exist" in result.stdout


def test_inspect_ai_rejects_empty_snapshot_before_agent_call(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "fake-key")

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("agent must not be called")

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", unexpected_call
    )

    result = runner.invoke(app, ["inspect", str(tmp_path), "--ai"])

    assert result.exit_code == 1
    assert "snapshot is empty" in result.stdout
    assert "no API request was made" in result.stdout
