"""Docker CLI boundary for isolated, resource-bounded command execution."""

from __future__ import annotations

import codecs
import os
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Protocol

from fanatic_agents.sandbox.errors import (
    DockerDaemonUnavailableError,
    DockerUnavailableError,
    SandboxExecutionError,
    SandboxImageUnavailableError,
)
from fanatic_agents.sandbox.models import (
    DockerCheckResult,
    SandboxCommand,
    SandboxCommandResult,
    SandboxLimits,
    WorkspaceLimits,
)
from fanatic_agents.sandbox.policy import CommandPolicy, validate_image_reference
from fanatic_agents.sandbox.workspace import WorkspacePreparer


DOCKER_PREFLIGHT_TIMEOUT_SECONDS = 10.0
DOCKER_CLEANUP_TIMEOUT_SECONDS = 10.0

RunProcess = Callable[..., subprocess.CompletedProcess[str]]
PopenFactory = Callable[..., subprocess.Popen[bytes]]
WhichExecutable = Callable[[str], str | None]


class SandboxDockerBackend(Protocol):
    """Injectable Docker boundary used by the orchestration service."""

    def check(self) -> DockerCheckResult: ...

    def ensure_image(self, image: str) -> None: ...

    def execute(
        self,
        workspace: Path,
        image: str,
        command: SandboxCommand,
        limits: SandboxLimits,
    ) -> SandboxCommandResult: ...


@dataclass(slots=True)
class _Capture:
    text: str = ""
    truncated: bool = False
    failed: bool = False


class DockerCli:
    """Perform preflight and execution through argv-only Docker CLI calls."""

    def __init__(
        self,
        *,
        run_process: RunProcess | None = None,
        popen_factory: PopenFactory | None = None,
        which: WhichExecutable | None = None,
    ) -> None:
        self._run_process = run_process or subprocess.run
        self._popen_factory = popen_factory or subprocess.Popen
        self._which = which or shutil.which
        self._docker_executable: str | None = None

    def check(self) -> DockerCheckResult:
        """Require an installed CLI and a responsive daemon."""
        executable = self._resolve_executable()
        try:
            result = self._run_process(
                [executable, "info", "--format", "{{.ServerVersion}}"],
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise DockerDaemonUnavailableError(
                "Docker daemon is unavailable or did not respond."
            ) from exc
        if result.returncode != 0:
            raise DockerDaemonUnavailableError("Docker daemon is unavailable.")
        return DockerCheckResult(executable=executable, daemon_available=True)

    def ensure_image(self, image: str) -> None:
        """Require a local image without ever pulling it."""
        image = validate_image_reference(image)
        executable = self.check().executable
        try:
            result = self._run_process(
                [executable, "image", "inspect", image],
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=DOCKER_PREFLIGHT_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxImageUnavailableError(
                "Sandbox image availability could not be checked."
            ) from exc
        if result.returncode != 0:
            raise SandboxImageUnavailableError(
                "Sandbox image is not available locally. Pull or build the image "
                "explicitly before execution."
            )

    def execute(
        self,
        workspace: Path,
        image: str,
        command: SandboxCommand,
        limits: SandboxLimits,
    ) -> SandboxCommandResult:
        """Run Docker, stream bounded output, and force cleanup on timeout."""
        executable = self._resolve_executable()
        container_name = f"fanatic-agents-{uuid.uuid4().hex[:12]}"
        argv = build_docker_run_argv(
            executable=executable,
            workspace=workspace,
            image=image,
            command=command,
            limits=limits,
            container_name=container_name,
        )
        started = time.monotonic()
        try:
            process = self._popen_factory(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise DockerUnavailableError("Docker CLI is not available.") from exc
        except OSError as exc:
            raise SandboxExecutionError("Sandbox execution could not be started.") from exc

        if process.stdout is None or process.stderr is None:
            self._force_remove(container_name)
            raise SandboxExecutionError("Sandbox output could not be captured.")

        stdout_capture = _Capture()
        stderr_capture = _Capture()
        stdout_thread = threading.Thread(
            target=_read_bounded_output,
            args=(process.stdout, limits.max_stdout_characters, stdout_capture),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_read_bounded_output,
            args=(process.stderr, limits.max_stderr_characters, stderr_capture),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        timed_out = False
        try:
            process.wait(timeout=limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._force_remove(container_name)
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=DOCKER_CLEANUP_TIMEOUT_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
        except (OSError, subprocess.SubprocessError) as exc:
            self._force_remove(container_name)
            raise SandboxExecutionError("Sandbox execution could not be completed.") from exc
        finally:
            _finish_capture(process.stdout, stdout_thread)
            _finish_capture(process.stderr, stderr_thread)

        if stdout_capture.failed or stderr_capture.failed:
            self._force_remove(container_name)
            raise SandboxExecutionError("Sandbox output could not be captured.")

        duration = time.monotonic() - started
        return SandboxCommandResult(
            argv=command.argv,
            exit_code=None if timed_out else process.returncode,
            stdout=stdout_capture.text,
            stderr=stderr_capture.text,
            duration_seconds=float(duration),
            timed_out=timed_out,
            stdout_truncated=stdout_capture.truncated,
            stderr_truncated=stderr_capture.truncated,
            container_name=container_name,
        )

    def _resolve_executable(self) -> str:
        if self._docker_executable is None:
            executable = self._which("docker")
            if executable is None:
                raise DockerUnavailableError("Docker CLI is not available.")
            self._docker_executable = executable
        return self._docker_executable

    def _force_remove(self, container_name: str) -> None:
        try:
            executable = self._resolve_executable()
            self._run_process(
                [executable, "rm", "--force", container_name],
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=DOCKER_CLEANUP_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.SubprocessError):
            return


class DockerSandbox:
    """Coordinate policy, safe copying, Docker preflight, and execution."""

    def __init__(
        self,
        *,
        docker: SandboxDockerBackend | None = None,
        policy: CommandPolicy | None = None,
        workspace_preparer: WorkspacePreparer | None = None,
    ) -> None:
        self._docker = docker or DockerCli()
        self._policy = policy or CommandPolicy()
        self._workspace_preparer = workspace_preparer or WorkspacePreparer()

    def run(
        self,
        repository: Path,
        image: str,
        command: SandboxCommand,
        *,
        limits: SandboxLimits | None = None,
    ) -> SandboxCommandResult:
        """Execute only against a disposable workspace copy."""
        image = validate_image_reference(image)
        command = self._policy.validate(command)
        effective_limits = limits or SandboxLimits()
        with tempfile.TemporaryDirectory(prefix="fanatic-agents-") as temporary:
            workspace = Path(temporary) / "workspace"
            prepared = self._workspace_preparer.prepare(repository, workspace)
            self._docker.ensure_image(image)
            return self._docker.execute(prepared.path, image, command, effective_limits)


def build_docker_run_argv(
    *,
    executable: str,
    workspace: Path,
    image: str,
    command: SandboxCommand,
    limits: SandboxLimits,
    container_name: str,
    user: str | None = None,
) -> list[str]:
    """Build a hardened docker run argv without consulting host secrets."""
    image = validate_image_reference(image)
    numeric_user = user
    if numeric_user is None and hasattr(os, "getuid") and hasattr(os, "getgid"):
        numeric_user = f"{os.getuid()}:{os.getgid()}"
    argv = [
        executable,
        "run",
        "--rm",
        "--pull",
        "never",
        "--name",
        container_name,
        "--network",
        "none",
        "--memory",
        f"{limits.memory_mb}m",
        "--cpus",
        str(limits.cpus),
        "--pids-limit",
        str(limits.pids_limit),
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=64m",
        "--mount",
        f"type=bind,source={workspace},target=/workspace",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp",
        "--env",
        "PYTHONDONTWRITEBYTECODE=1",
    ]
    if numeric_user is not None:
        argv.extend(["--user", numeric_user])
    argv.extend([image, *command.argv])
    return argv


def _read_bounded_output(stream: BinaryIO, limit: int, capture: _Capture) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    parts: list[str] = []
    characters = 0
    try:
        while raw := stream.read(8_192):
            decoded = decoder.decode(raw)
            remaining = limit - characters
            if remaining > 0:
                kept = decoded[:remaining]
                parts.append(kept)
                characters += len(kept)
            if len(decoded) > max(remaining, 0):
                capture.truncated = True
        tail = decoder.decode(b"", final=True)
        remaining = limit - characters
        if remaining > 0:
            parts.append(tail[:remaining])
        if len(tail) > max(remaining, 0):
            capture.truncated = True
        capture.text = "".join(parts)
    except (OSError, ValueError):
        capture.failed = True


def _finish_capture(stream: BinaryIO, thread: threading.Thread) -> None:
    thread.join(timeout=DOCKER_CLEANUP_TIMEOUT_SECONDS)
    if thread.is_alive():
        try:
            stream.close()
        except OSError:
            pass
        thread.join(timeout=1.0)


def check_docker_sandbox() -> DockerCheckResult:
    """Public API used by the CLI for Docker preflight."""
    return DockerCli().check()


def run_sandbox_command(
    repository: Path,
    image: str,
    command: SandboxCommand,
    *,
    limits: SandboxLimits | None = None,
    workspace_limits: WorkspaceLimits | None = None,
) -> SandboxCommandResult:
    """Public API for one manual, isolated sandbox command."""
    preparer = WorkspacePreparer(workspace_limits)
    return DockerSandbox(workspace_preparer=preparer).run(
        repository, image, command, limits=limits
    )
