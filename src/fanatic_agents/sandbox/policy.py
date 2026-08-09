"""Explicit deny-by-default command and image policy."""

from __future__ import annotations

import re
import shlex

from pydantic import ValidationError

from fanatic_agents.sandbox.errors import SandboxPolicyError
from fanatic_agents.sandbox.models import SandboxCommand


DEFAULT_ALLOWED_EXECUTABLES = frozenset({
    "python",
    "python3",
    "pytest",
    "node",
    "npm",
    "npx",
    "pnpm",
    "yarn",
    "flutter",
    "dart",
    "mvn",
    "mvnw",
    "gradle",
    "gradlew",
    "./gradlew",
    "./mvnw",
    "go",
    "cargo",
    "make",
})
DENIED_EXECUTABLES = frozenset({
    "sh",
    "bash",
    "zsh",
    "fish",
    "cmd",
    "cmd.exe",
    "powershell",
    "powershell.exe",
    "pwsh",
    "docker",
    "podman",
    "ssh",
})
SHELL_CONSTRUCTIONS = ("&&", "||", ";", "|", ">>", ">", "<", "`", "$(")
IMAGE_REFERENCE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@-]*$")


class CommandPolicy:
    """Validate commands against an explicit executable allowlist."""

    def __init__(self, allowed_executables: frozenset[str] | None = None) -> None:
        self._allowed_executables = (
            DEFAULT_ALLOWED_EXECUTABLES if allowed_executables is None else allowed_executables
        )

    def validate(self, command: SandboxCommand) -> SandboxCommand:
        """Return a validated command or raise a user-safe policy error."""
        executable = command.argv[0]
        normalized = executable.lower()
        if normalized in DENIED_EXECUTABLES:
            raise SandboxPolicyError(f"Executable is explicitly denied: {executable}")
        if normalized not in self._allowed_executables:
            raise SandboxPolicyError(f"Executable is not allowed: {executable}")

        for argument in command.argv:
            if "\x00" in argument or any(marker in argument for marker in SHELL_CONSTRUCTIONS):
                raise SandboxPolicyError("Shell operators and substitutions are not allowed.")

        arguments = command.argv[1:]
        if normalized in {"python", "python3"} and any(
            argument == "-c" or argument.startswith("-c") for argument in arguments
        ):
            raise SandboxPolicyError("Inline Python execution is not allowed.")
        if normalized == "node" and any(
            argument in {"-e", "--eval"}
            or argument.startswith("--eval=")
            or (argument.startswith("-e") and argument != "--")
            for argument in arguments
        ):
            raise SandboxPolicyError("Inline Node.js execution is not allowed.")
        return command


def parse_command(command_text: str) -> SandboxCommand:
    """Parse CLI shell-like text once, producing a shell-free command model."""
    try:
        argv = shlex.split(command_text, posix=True)
    except ValueError as exc:
        raise SandboxPolicyError("Command contains invalid quoting.") from exc
    if not argv:
        raise SandboxPolicyError("Command must not be empty.")
    try:
        return SandboxCommand(argv=argv)
    except ValidationError as exc:
        raise SandboxPolicyError("Command contains an empty argument.") from exc


def validate_image_reference(image: str) -> str:
    """Reject ambiguous values that Docker could interpret as CLI options."""
    value = image.strip()
    if not value or not IMAGE_REFERENCE_PATTERN.fullmatch(value):
        raise SandboxPolicyError("Sandbox image reference is invalid.")
    return value
