"""Project configuration loading and validation."""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field

from fanatic_agents.core.limits import LimitsConfig
from fanatic_agents.core.project import (
    NonEmptyStrictString,
    ProjectInfo,
    RepositoryConfig,
    StrictModel,
)


class ConfigLoadError(ValueError):
    """Raised when a configuration file cannot be read or parsed."""


class CommandConfig(StrictModel):
    """Commands used to prepare, test, and build a project."""

    setup: list[NonEmptyStrictString] = Field(default_factory=list)
    test: list[NonEmptyStrictString] = Field(default_factory=list)
    build: list[NonEmptyStrictString] = Field(default_factory=list)


class PermissionsConfig(StrictModel):
    """Explicit capabilities; every permission defaults to denied."""

    read_repository: bool = False
    create_branch: bool = False
    modify_files: bool = False
    run_commands: bool = False
    commit: bool = False
    push_branch: bool = False
    create_pull_request: bool = False

    merge: bool = False
    production_deploy: bool = False
    modify_secrets: bool = False
    destructive_database_changes: bool = False


class ProjectConfig(StrictModel):
    """Complete, validated configuration for one managed project."""

    project: ProjectInfo
    repository: RepositoryConfig
    commands: CommandConfig
    limits: LimitsConfig
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)


def load_project_config(path: str | Path) -> ProjectConfig:
    """Load a YAML file and validate it as a :class:`ProjectConfig`."""

    config_path = Path(path)
    try:
        raw_text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigLoadError(f"Could not read '{config_path}': {exc}") from exc

    try:
        raw_config: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigLoadError(f"Invalid YAML in '{config_path}': {exc}") from exc

    if not isinstance(raw_config, dict):
        raise ConfigLoadError(
            f"Configuration '{config_path}' must contain a YAML mapping at its root."
        )

    return ProjectConfig.model_validate(raw_config)

