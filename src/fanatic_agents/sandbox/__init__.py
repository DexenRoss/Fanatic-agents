"""Controlled Docker sandbox public API."""

from fanatic_agents.sandbox.docker import (
    DockerCli,
    DockerSandbox,
    build_docker_run_argv,
    check_docker_sandbox,
    run_sandbox_command,
)
from fanatic_agents.sandbox.errors import (
    DockerDaemonUnavailableError,
    DockerUnavailableError,
    SandboxError,
    SandboxExecutionError,
    SandboxImageUnavailableError,
    SandboxPolicyError,
    SandboxWorkspaceError,
)
from fanatic_agents.sandbox.models import (
    DockerCheckResult,
    SandboxCommand,
    SandboxCommandResult,
    SandboxLimits,
    WorkspaceLimits,
)
from fanatic_agents.sandbox.policy import CommandPolicy, parse_command

__all__ = [
    "CommandPolicy",
    "DockerCheckResult",
    "DockerCli",
    "DockerDaemonUnavailableError",
    "DockerSandbox",
    "DockerUnavailableError",
    "SandboxCommand",
    "SandboxCommandResult",
    "SandboxError",
    "SandboxExecutionError",
    "SandboxImageUnavailableError",
    "SandboxLimits",
    "SandboxPolicyError",
    "SandboxWorkspaceError",
    "WorkspaceLimits",
    "build_docker_run_argv",
    "check_docker_sandbox",
    "parse_command",
    "run_sandbox_command",
]
"""Future sandbox integration."""

