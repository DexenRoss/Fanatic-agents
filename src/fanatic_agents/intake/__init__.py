"""Safe, deterministic GitHub Issue task intake."""

from fanatic_agents.intake.models import (
    GitHubIssueCandidate,
    TaskDiscoveryResult,
    TaskIntakeReceipt,
    TaskIntakeResult,
    TaskSpec,
)
from fanatic_agents.intake.policy import TaskIntakePolicy
from fanatic_agents.intake.service import (
    TaskIntakeService,
    discover_tasks,
    select_task,
)

__all__ = [
    "GitHubIssueCandidate",
    "TaskDiscoveryResult",
    "TaskIntakePolicy",
    "TaskIntakeReceipt",
    "TaskIntakeResult",
    "TaskIntakeService",
    "TaskSpec",
    "discover_tasks",
    "select_task",
]
