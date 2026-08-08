"""Operational limits for a Fanatic Agents project."""

from typing import Annotated

from pydantic import Field

from fanatic_agents.core.project import StrictModel


PositiveStrictInt = Annotated[int, Field(strict=True, gt=0)]
PositiveStrictFloat = Annotated[float, Field(strict=True, gt=0)]


class LimitsConfig(StrictModel):
    """Positive resource and iteration limits configured per project."""

    max_tasks_per_day: PositiveStrictInt
    max_runtime_minutes: PositiveStrictInt
    max_daily_cost_usd: PositiveStrictFloat
    max_iterations_per_task: PositiveStrictInt

