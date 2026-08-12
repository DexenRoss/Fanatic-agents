"""Lifetime control for one filtered implementation workspace."""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import TracebackType

from fanatic_agents.sandbox.workspace import PreparedWorkspace, WorkspacePreparer


class TemporaryImplementationWorkspace:
    """Own a filtered copy from preparation through verification and cleanup."""

    def __init__(
        self,
        repository: Path,
        *,
        preparer: WorkspacePreparer | None = None,
    ) -> None:
        self._repository = Path(repository)
        self._preparer = preparer or WorkspacePreparer()
        self._temporary: tempfile.TemporaryDirectory[str] | None = None
        self.prepared: PreparedWorkspace | None = None

    def __enter__(self) -> PreparedWorkspace:
        self._temporary = tempfile.TemporaryDirectory(prefix="fanatic-agents-implementation-")
        destination = Path(self._temporary.name) / "workspace"
        self.prepared = self._preparer.prepare(self._repository, destination)
        return self.prepared

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()
        self._temporary = None
