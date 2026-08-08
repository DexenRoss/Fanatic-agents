"""Read-only Git and repository intelligence."""

from fanatic_agents.git.inspection import (
    RepositoryInspectionError,
    RepositoryInspector,
    RepositorySnapshot,
    SnapshotFile,
    SnapshotLimits,
    SnapshotTruncation,
)

__all__ = [
    "RepositoryInspectionError",
    "RepositoryInspector",
    "RepositorySnapshot",
    "SnapshotFile",
    "SnapshotLimits",
    "SnapshotTruncation",
]
