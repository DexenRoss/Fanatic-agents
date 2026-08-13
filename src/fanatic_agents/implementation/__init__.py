"""Controlled implementation over disposable repository workspaces."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fanatic_agents.implementation.models import (
    AppliedChange,
    ChangeOperation,
    ChangeSet,
    ImplementationResult,
    WorkspaceSummary,
)

if TYPE_CHECKING:
    from fanatic_agents.implementation.service import ControlledImplementationService


def __getattr__(name: str) -> Any:
    if name == "ControlledImplementationService":
        from fanatic_agents.implementation.service import (
            ControlledImplementationService,
        )

        return ControlledImplementationService
    raise AttributeError(name)


__all__ = [
    "AppliedChange",
    "ChangeOperation",
    "ChangeSet",
    "ControlledImplementationService",
    "ImplementationResult",
    "WorkspaceSummary",
]
