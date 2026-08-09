"""Preparation of an isolated, filtered, and bounded repository copy."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from fanatic_agents.core.path_safety import (
    is_excluded_directory,
    is_probably_binary,
    is_secret_path,
)
from fanatic_agents.sandbox.errors import SandboxWorkspaceError
from fanatic_agents.sandbox.models import WorkspaceLimits


@dataclass(frozen=True, slots=True)
class PreparedWorkspace:
    """Metadata about a completed safe workspace copy."""

    path: Path
    file_count: int
    total_bytes: int


class WorkspacePreparer:
    """Copy only safe regular text files into a dedicated destination."""

    def __init__(self, limits: WorkspaceLimits | None = None) -> None:
        self._limits = limits or WorkspaceLimits()

    def prepare(self, repository: Path, destination: Path) -> PreparedWorkspace:
        """Create a bounded copy without following repository symlinks."""
        source = Path(repository).expanduser()
        if not source.exists():
            raise SandboxWorkspaceError(f"Repository path does not exist: {source}")
        if not source.is_dir():
            raise SandboxWorkspaceError(f"Repository path is not a directory: {source}")
        source = source.resolve()

        target = Path(destination)
        if target.exists() and any(target.iterdir()):
            raise SandboxWorkspaceError("Temporary workspace destination is not empty.")
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SandboxWorkspaceError("Temporary workspace could not be created.") from exc

        file_count = 0
        total_bytes = 0
        try:
            for current_root, directory_names, file_names in os.walk(
                source, topdown=True, followlinks=False
            ):
                current = Path(current_root)
                directory_names[:] = sorted(
                    name
                    for name in directory_names
                    if self._include_directory(source, current / name)
                )
                for file_name in sorted(file_names):
                    file_path = current / file_name
                    relative = file_path.relative_to(source)
                    if not self._include_file(source, file_path, relative):
                        continue
                    source_stat = file_path.stat(follow_symlinks=False)
                    if source_stat.st_size > self._limits.max_file_bytes:
                        raise SandboxWorkspaceError(
                            f"Workspace file exceeds max_file_bytes: {relative.as_posix()}"
                        )
                    if file_count + 1 > self._limits.max_files:
                        raise SandboxWorkspaceError("Workspace exceeds max_files.")
                    if total_bytes + source_stat.st_size > self._limits.max_total_bytes:
                        raise SandboxWorkspaceError("Workspace exceeds max_total_bytes.")

                    copied_bytes = self._copy_file(
                        source,
                        file_path,
                        target / relative,
                        total_bytes=total_bytes,
                    )
                    file_count += 1
                    total_bytes += copied_bytes
        except SandboxWorkspaceError:
            raise
        except OSError as exc:
            raise SandboxWorkspaceError("Repository could not be copied safely.") from exc

        return PreparedWorkspace(path=target, file_count=file_count, total_bytes=total_bytes)

    @staticmethod
    def _include_directory(source: Path, path: Path) -> bool:
        if path.is_symlink() or is_excluded_directory(path.name):
            return False
        try:
            relative = path.relative_to(source)
        except ValueError:
            return False
        return not is_secret_path(relative)

    @staticmethod
    def _include_file(source: Path, path: Path, relative: Path) -> bool:
        if path.is_symlink() or is_secret_path(relative):
            return False
        try:
            file_stat = path.stat(follow_symlinks=False)
            resolved = path.resolve(strict=True)
            resolved.relative_to(source)
        except (OSError, ValueError):
            return False
        return stat.S_ISREG(file_stat.st_mode) and not is_probably_binary(resolved)

    def _copy_file(
        self,
        source: Path,
        file_path: Path,
        destination: Path,
        *,
        total_bytes: int,
    ) -> int:
        resolved = file_path.resolve(strict=True)
        try:
            resolved.relative_to(source)
        except ValueError as exc:
            raise SandboxWorkspaceError("Repository link escapes its root.") from exc

        destination.parent.mkdir(parents=True, exist_ok=True)
        copied = 0
        try:
            with resolved.open("rb") as source_file, destination.open("xb") as target_file:
                while chunk := source_file.read(64 * 1024):
                    copied += len(chunk)
                    if copied > self._limits.max_file_bytes:
                        raise SandboxWorkspaceError(
                            f"Workspace file exceeds max_file_bytes: "
                            f"{file_path.relative_to(source).as_posix()}"
                        )
                    if total_bytes + copied > self._limits.max_total_bytes:
                        raise SandboxWorkspaceError("Workspace exceeds max_total_bytes.")
                    target_file.write(chunk)
            mode = stat.S_IMODE(file_path.stat(follow_symlinks=False).st_mode) & 0o777
            destination.chmod(mode)
        except SandboxWorkspaceError:
            raise
        except OSError as exc:
            raise SandboxWorkspaceError("A repository file could not be copied safely.") from exc
        return copied
