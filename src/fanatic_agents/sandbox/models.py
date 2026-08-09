"""Structured models for bounded sandbox execution."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field, StringConstraints

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel


PositiveInt = Annotated[int, Field(strict=True, gt=0)]
PositiveFloat = Annotated[float, Field(strict=True, gt=0)]
CommandArgument = Annotated[str, StringConstraints(strict=True, min_length=1)]


class SandboxCommand(StrictModel):
    """A command represented only as an argument vector, never as shell text."""

    argv: list[CommandArgument] = Field(min_length=1)


class SandboxLimits(StrictModel):
    """Runtime and captured-output limits for one container execution."""

    timeout_seconds: PositiveFloat = 120.0
    memory_mb: PositiveInt = 1024
    cpus: PositiveFloat = 1.0
    pids_limit: PositiveInt = 256
    max_stdout_characters: PositiveInt = 20_000
    max_stderr_characters: PositiveInt = 20_000


class WorkspaceLimits(StrictModel):
    """Bounds applied while making the temporary repository copy."""

    max_files: PositiveInt = 10_000
    max_total_bytes: PositiveInt = 100 * 1024 * 1024
    max_file_bytes: PositiveInt = 10 * 1024 * 1024


class SandboxCommandResult(StrictModel):
    """Bounded result suitable for future deterministic agent consumption."""

    argv: list[CommandArgument] = Field(min_length=1)
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float = Field(strict=True, ge=0)
    timed_out: bool
    stdout_truncated: bool
    stderr_truncated: bool
    container_name: str | None = None


class DockerCheckResult(StrictModel):
    """Successful Docker CLI and daemon preflight details."""

    executable: NonEmptyStrictString
    daemon_available: bool
