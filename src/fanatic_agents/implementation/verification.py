"""Verification boundary for an already-prepared implementation workspace."""
from __future__ import annotations
from pathlib import Path
from fanatic_agents.sandbox.docker import DockerCli, SandboxDockerBackend
from fanatic_agents.sandbox.errors import SandboxExecutionError
from fanatic_agents.sandbox.models import SandboxCommand, SandboxCommandResult, SandboxLimits
from fanatic_agents.sandbox.policy import CommandPolicy, validate_image_reference
from fanatic_agents.sandbox.workspace import PreparedWorkspace

class PreparedWorkspaceSandbox:
    """Verify only a typed workspace produced by WorkspacePreparer."""
    def __init__(self, *, docker: SandboxDockerBackend | None = None, policy: CommandPolicy | None = None) -> None:
        self._docker = docker or DockerCli()
        self._policy = policy or CommandPolicy()

    def run_prepared_workspace(self, workspace: PreparedWorkspace, image: str, command: SandboxCommand, *, limits: SandboxLimits | None = None) -> SandboxCommandResult:
        """Run one revalidated command against the modified temporary copy."""
        image = validate_image_reference(image)
        command = self._policy.validate(command)
        path = Path(workspace.path)
        if path.is_symlink() or not path.is_dir():
            raise SandboxExecutionError("Prepared workspace is unavailable or unsafe.")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise SandboxExecutionError("Prepared workspace is unavailable or unsafe.") from exc
        self._docker.ensure_image(image)
        return self._docker.execute(resolved, image, command, limits or SandboxLimits())