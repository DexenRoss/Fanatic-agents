"""Tests for centralized, secret-safe application settings."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from fanatic_agents.agents import _shared
from fanatic_agents.agents.developer import DeveloperAssessment
from fanatic_agents.cli.main import app
from fanatic_agents.core.settings import ApplicationSettings, get_settings

runner = CliRunner()


def _clear_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("FANATIC_AGENTS_MODEL", raising=False)


def test_settings_load_from_current_directory_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "dotenv-secret-value"
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret}\nFANATIC_AGENTS_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = get_settings()

    assert settings.has_openai_api_key is True
    assert settings.openai_api_key_value() == secret
    assert settings.fanatic_agents_model == "dotenv-model"
    assert secret not in repr(settings)
    assert secret not in str(settings.model_dump())
    assert secret not in settings.model_dump_json()
    assert "openai_api_key" not in settings.model_dump()


def test_environment_variables_override_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=dotenv-key\nFANATIC_AGENTS_MODEL=dotenv-model\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "environment-key")
    monkeypatch.setenv("FANATIC_AGENTS_MODEL", "environment-model")
    configured_keys: list[str] = []
    monkeypatch.setattr(
        _shared,
        "set_default_openai_key",
        lambda key: configured_keys.append(key),
    )

    settings = get_settings()
    configured = _shared.configure_openai_sdk(settings)

    assert settings.openai_api_key_value() == "environment-key"
    assert settings.fanatic_agents_model == "environment-model"
    assert configured is True
    assert configured_keys == ["environment-key"]


def test_blank_or_missing_settings_remain_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "OPENAI_API_KEY=   \nFANATIC_AGENTS_MODEL=\n",
        encoding="utf-8",
    )
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    settings = get_settings()

    assert settings.has_openai_api_key is False
    assert settings.openai_api_key_value() is None
    assert settings.fanatic_agents_model is None


def test_dotenv_key_configures_sdk_without_exposing_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "provider-secret-value"
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret}\n", encoding="utf-8"
    )
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    calls: list[str] = []
    monkeypatch.setattr(
        _shared,
        "set_default_openai_key",
        lambda key: calls.append(key),
    )

    configured = _shared.configure_openai_sdk(get_settings())

    assert configured is True
    assert calls == [secret]
    captured = capsys.readouterr()
    assert secret not in captured.out
    assert secret not in captured.err


def test_sdk_configuration_exception_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "exception-secret-value"
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret}\n", encoding="utf-8"
    )
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def fail_safely(_key: str) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(_shared, "set_default_openai_key", fail_safely)

    with pytest.raises(_shared.OpenAIConfigurationError) as error:
        _shared.configure_openai_sdk(get_settings())

    assert secret not in str(error.value)


def test_inspect_ai_uses_dotenv_key_without_exposing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "dotenv-cli-secret"
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Local", encoding="utf-8")
    (tmp_path / ".env").write_text(
        f"OPENAI_API_KEY={secret}\n", encoding="utf-8"
    )
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)
    events: list[str] = []
    monkeypatch.setattr(
        "fanatic_agents.cli.main.configure_openai_sdk",
        lambda _settings: events.append("configure"),
    )

    def fake_runner(_snapshot):
        events.append("runner")
        return DeveloperAssessment(
            summary="Bounded summary.",
            architecture="One documented component.",
            readiness="ready",
        )

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", fake_runner
    )

    result = runner.invoke(app, ["inspect", str(repository), "--ai"])

    assert result.exit_code == 0
    assert events == ["configure", "runner"]
    assert secret not in result.stdout


def test_missing_dotenv_key_still_stops_before_agent_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "README.md").write_text("# Local", encoding="utf-8")
    _clear_settings_environment(monkeypatch)
    monkeypatch.chdir(tmp_path)

    def unexpected_call(*_args, **_kwargs):
        raise AssertionError("agent must not be called without a configured key")

    monkeypatch.setattr(
        "fanatic_agents.cli.main.run_developer_assessment", unexpected_call
    )
    monkeypatch.setattr(
        "fanatic_agents.cli.main.configure_openai_sdk", unexpected_call
    )

    result = runner.invoke(app, ["inspect", str(repository), "--ai"])

    assert result.exit_code == 1
    assert "requires OPENAI_API_KEY" in result.stdout
    assert "no API request was made" in result.stdout
