"""Tests for strict project configuration models."""

from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from fanatic_agents.core.config import PermissionsConfig, ProjectConfig, load_project_config


def test_valid_project_config(valid_config_data: dict[str, object]) -> None:
    config = ProjectConfig.model_validate(valid_config_data)

    assert config.project.name == "example-project"
    assert config.repository.main_branch == "main"
    assert config.limits.max_daily_cost_usd == 5.0
    assert config.intake.enabled is False
    assert config.intake.required_labels == ["fanatic:ready"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tasks_per_day", -10),
        ("max_runtime_minutes", 0),
        ("max_daily_cost_usd", -0.01),
        ("max_iterations_per_task", -1),
    ],
)
def test_non_positive_limits_are_rejected(
    valid_config_data: dict[str, object], field: str, value: int | float
) -> None:
    invalid_data = deepcopy(valid_config_data)
    limits = invalid_data["limits"]
    assert isinstance(limits, dict)
    limits[field] = value

    with pytest.raises(ValidationError, match=field):
        ProjectConfig.model_validate(invalid_data)


def test_permission_defaults_are_safe() -> None:
    permissions = PermissionsConfig()

    assert permissions.merge is False
    assert permissions.production_deploy is False
    assert permissions.modify_secrets is False
    assert permissions.destructive_database_changes is False
    assert permissions.read_issues is False
    assert not any(permissions.model_dump().values())


def test_unknown_fields_are_rejected(valid_config_data: dict[str, object]) -> None:
    invalid_data = deepcopy(valid_config_data)
    invalid_data["unexpected"] = True

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ProjectConfig.model_validate(invalid_data)


def test_example_yaml_loads(project_root: Path) -> None:
    config = load_project_config(project_root / "projects" / "example.yaml")

    assert config.project.name == "example-project"
    assert config.commands.test == ['echo "tests"']

