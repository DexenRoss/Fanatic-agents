"""Tests for controlled, isolated Docker sandbox execution."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from fanatic_agents.cli.main import app
from fanatic_agents.sandbox.docker import (
    DockerCli,
    DockerSandbox,
    build_docker_run_argv,
)
from fanatic_agents.sandbox.errors import (
    DockerDaemonUnavailableError,
    DockerUnavailableError,
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
from fanatic_agents.sandbox.workspace import WorkspacePreparer


runner = CliRunner()


def _completed(returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], returncode, stdout="", stderr="")


def _result(
    *,
    stdout: str = "ok\n",
    stderr: str = "",
    stdout_truncated: bool = False,
) -> SandboxCommandResult:
    return SandboxCommandResult(
        argv=["python", "--version"],
        exit_code=0,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0.1,
        timed_out=False,
        stdout_truncated=stdout_truncated,
        stderr_truncated=False,
        container_name="fanatic-agents-test",
    )


def test_sandbox_limits_defaults_are_valid() -> None:
    limits = SandboxLimits()

    assert limits.timeout_seconds == 120.0
    assert limits.memory_mb == 1024
    assert limits.cpus == 1.0
    assert limits.pids_limit == 256


@pytest.mark.parametrize(
    "field",
    [
        "timeout_seconds",
        "memory_mb",
        "cpus",
        "pids_limit",
        "max_stdout_characters",
        "max_stderr_characters",
    ],
)
def test_sandbox_limits_reject_non_positive_values(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        SandboxLimits.model_validate({field: 0})


def test_command_policy_accepts_allowed_command() -> None:
    command = SandboxCommand(argv=["python", "-m", "pytest"])

    assert CommandPolicy().validate(command) is command


@pytest.mark.parametrize("executable", ["bash", "sh", "pwsh", "docker", "ssh"])
def test_command_policy_rejects_denied_executable(executable: str) -> None:
    with pytest.raises(SandboxPolicyError, match="denied"):
        CommandPolicy().validate(SandboxCommand(argv=[executable]))


@pytest.mark.parametrize("operator", [";", "&&", "||", "|", ">", ">>", "<", "`", "$(id)"])
def test_command_policy_rejects_shell_constructions(operator: str) -> None:
    with pytest.raises(SandboxPolicyError, match="Shell operators"):
        CommandPolicy().validate(SandboxCommand(argv=["pytest", operator]))


@pytest.mark.parametrize(
    "argv",
    [
        ["python", "-c", "print('unsafe')"],
        ["python3", "-cprint('unsafe')"],
        ["node", "-e", "console.log('unsafe')"],
        ["node", "--eval=console.log('unsafe')"],
    ],
)
def test_command_policy_rejects_inline_execution(argv: list[str]) -> None:
    with pytest.raises(SandboxPolicyError, match="Inline"):
        CommandPolicy().validate(SandboxCommand(argv=argv))


def test_cli_command_parse_happens_once_into_argv() -> None:
    command = parse_command('python -m pytest "tests/unit test.py"')

    assert command.argv == ["python", "-m", "pytest", "tests/unit test.py"]


def test_cli_command_parse_rejects_invalid_quoting() -> None:
    with pytest.raises(SandboxPolicyError, match="quoting"):
        parse_command('python "unterminated')


def test_workspace_excludes_secrets_dependencies_git_and_binary(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    destination = tmp_path / "copy"
    repository.mkdir()
    (repository / "safe.py").write_text("SAFE = True\n", encoding="utf-8")
    (repository / ".env").write_text("OPENAI_API_KEY=never", encoding="utf-8")
    (repository / ".env.local").write_text("TOKEN=never", encoding="utf-8")
    (repository / "api-key.txt").write_text("never", encoding="utf-8")
    (repository / "private_key.pem").write_text("never", encoding="utf-8")
    (repository / "asset.bin").write_bytes(b"safe\x00binary")
    for directory in ("node_modules", ".git", "build"):
        path = repository / directory / "nested.txt"
        path.parent.mkdir(parents=True)
        path.write_text("excluded", encoding="utf-8")

    prepared = WorkspacePreparer().prepare(repository, destination)

    assert prepared.file_count == 1
    assert (destination / "safe.py").is_file()
    copied_paths = {path.relative_to(destination).as_posix() for path in destination.rglob("*")}
    assert copied_paths == {"safe.py"}


@pytest.mark.parametrize(
    ("limits", "files", "message"),
    [
        (WorkspaceLimits(max_files=1), {"a.txt": "a", "b.txt": "b"}, "max_files"),
        (
            WorkspaceLimits(max_total_bytes=3),
            {"a.txt": "aa", "b.txt": "bb"},
            "max_total_bytes",
        ),
        (WorkspaceLimits(max_file_bytes=1), {"a.txt": "aa"}, "max_file_bytes"),
    ],
)
def test_workspace_bounds_abort_copy(
    tmp_path: Path,
    limits: WorkspaceLimits,
    files: dict[str, str],
    message: str,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    for name, content in files.items():
        (repository / name).write_text(content, encoding="utf-8")

    with pytest.raises(SandboxWorkspaceError, match=message):
        WorkspacePreparer(limits).prepare(repository, tmp_path / "copy")


def test_workspace_does_not_follow_symlinks(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    outside_file = tmp_path / "outside.txt"
    outside_file.write_text("must not copy", encoding="utf-8")
    outside_directory = tmp_path / "outside"
    outside_directory.mkdir()
    (outside_directory / "data.txt").write_text("must not copy", encoding="utf-8")
    (repository / "file-link").symlink_to(outside_file)
    (repository / "directory-link").symlink_to(outside_directory, target_is_directory=True)

    prepared = WorkspacePreparer().prepare(repository, tmp_path / "copy")

    assert prepared.file_count == 0
    assert list(prepared.path.rglob("*")) == []


def test_sandbox_modifications_never_affect_original_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    original = repository / "source.py"
    original.write_text("original\n", encoding="utf-8")

    class MutatingBackend:
        workspace: Path | None = None

        def check(self) -> DockerCheckResult:
            return DockerCheckResult(executable="docker", daemon_available=True)

        def ensure_image(self, image: str) -> None:
            assert image == "python:3.12-slim"

        def execute(self, workspace, image, command, limits):
            self.workspace = workspace
            (workspace / "source.py").write_text("sandbox change\n", encoding="utf-8")
            (workspace / "new.txt").write_text("sandbox only\n", encoding="utf-8")
            return _result()

    backend = MutatingBackend()
    result = DockerSandbox(docker=backend).run(
        repository,
        "python:3.12-slim",
        SandboxCommand(argv=["python", "--version"]),
    )

    assert result.exit_code == 0
    assert original.read_text(encoding="utf-8") == "original\n"
    assert not (repository / "new.txt").exists()
    assert backend.workspace is not None
    assert not backend.workspace.exists()


def test_docker_missing_is_reported() -> None:
    with pytest.raises(DockerUnavailableError, match="not available"):
        DockerCli(which=lambda _: None).check()


def test_docker_daemon_unavailable_is_reported() -> None:
    docker = DockerCli(
        which=lambda _: "/usr/bin/docker",
        run_process=lambda *_a, **_k: _completed(1),
    )

    with pytest.raises(DockerDaemonUnavailableError, match="unavailable"):
        docker.check()


def test_docker_image_must_exist_locally() -> None:
    results = iter([_completed(0), _completed(1)])
    calls: list[list[str]] = []

    def run(argv, **_kwargs):
        calls.append(argv)
        return next(results)

    docker = DockerCli(which=lambda _: "/usr/bin/docker", run_process=run)

    with pytest.raises(SandboxImageUnavailableError, match="not available locally"):
        docker.ensure_image("python:3.12-slim")

    assert all("pull" not in call for call in calls)
    assert calls[-1] == ["/usr/bin/docker", "image", "inspect", "python:3.12-slim"]


def test_docker_command_contains_all_hardening_and_no_secrets(tmp_path: Path) -> None:
    argv = build_docker_run_argv(
        executable="docker",
        workspace=tmp_path,
        image="python:3.12-slim",
        command=SandboxCommand(argv=["python", "--version"]),
        limits=SandboxLimits(),
        container_name="fanatic-agents-test",
        user="1000:1000",
    )
    joined = " ".join(argv)

    for option, value in (
        ("--network", "none"),
        ("--memory", "1024m"),
        ("--cpus", "1.0"),
        ("--pids-limit", "256"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--workdir", "/workspace"),
        ("--user", "1000:1000"),
    ):
        index = argv.index(option)
        assert argv[index + 1] == value
    assert "--rm" in argv
    assert argv[argv.index("--pull") + 1] == "never"
    assert "--read-only" in argv
    assert "--tmpfs" in argv
    mount = argv[argv.index("--mount") + 1]
    assert mount == f"type=bind,source={tmp_path},target=/workspace"
    assert mount.startswith("type=bind,")
    assert f"source={tmp_path}" in mount
    assert "target=/workspace" in mount
    assert ",rw" not in mount
    assert "OPENAI_API_KEY" not in joined
    assert "docker.sock" not in joined
    assert ".ssh" not in joined


class FakeProcess:
    def __init__(
        self,
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        timeout_once: bool = False,
    ) -> None:
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.returncode = returncode
        self.timeout_once = timeout_once
        self.wait_calls = 0
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.timeout_once and self.wait_calls == 1:
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.returncode

    def poll(self) -> int | None:
        return None if self.timeout_once and not self.killed else self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def test_command_result_captures_and_truncates_output(tmp_path: Path) -> None:
    process = FakeProcess(stdout=b"abcdefgh", stderr=b"error text")
    popen_calls: list[tuple[list[str], dict[str, Any]]] = []

    def popen(argv, **kwargs):
        popen_calls.append((argv, kwargs))
        return process

    docker = DockerCli(which=lambda _: "docker", popen_factory=popen)
    limits = SandboxLimits(max_stdout_characters=4, max_stderr_characters=5)

    result = docker.execute(
        tmp_path,
        "python:3.12-slim",
        SandboxCommand(argv=["python", "--version"]),
        limits,
    )

    assert result.stdout == "abcd"
    assert result.stderr == "error"
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True
    assert result.timed_out is False
    assert popen_calls[0][1]["shell"] is False


def test_timeout_marks_result_and_forces_container_cleanup(tmp_path: Path) -> None:
    process = FakeProcess(stdout=b"partial", timeout_once=True)
    cleanup_calls: list[list[str]] = []

    def run(argv, **_kwargs):
        cleanup_calls.append(argv)
        return _completed(0)

    docker = DockerCli(
        which=lambda _: "docker",
        run_process=run,
        popen_factory=lambda *_a, **_k: process,
    )

    result = docker.execute(
        tmp_path,
        "python:3.12-slim",
        SandboxCommand(argv=["python", "--version"]),
        SandboxLimits(timeout_seconds=0.01),
    )

    assert result.timed_out is True
    assert result.exit_code is None
    assert process.killed is True
    assert any(call[1:3] == ["rm", "--force"] for call in cleanup_calls)
    assert result.container_name in cleanup_calls[0]


def test_cli_sandbox_check_with_mock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "fanatic_agents.cli.main.check_docker_sandbox",
        lambda: DockerCheckResult(executable="/usr/bin/docker", daemon_available=True),
    )

    result = runner.invoke(app, ["sandbox", "check"])

    assert result.exit_code == 0
    assert "Sandbox Check" in result.stdout
    assert "AVAILABLE" in result.stdout


def test_cli_sandbox_run_with_mock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[Path, str, SandboxCommand]] = []

    def run(repository: Path, image: str, command: SandboxCommand):
        calls.append((repository, image, command))
        return _result(stdout="Python 3.12\n", stdout_truncated=True)

    monkeypatch.setattr("fanatic_agents.cli.main.run_sandbox_command", run)

    result = runner.invoke(
        app,
        [
            "sandbox",
            "run",
            str(tmp_path),
            "--image",
            "python:3.12-slim",
            "--command",
            "python --version",
        ],
    )

    assert result.exit_code == 0
    assert calls[0][2].argv == ["python", "--version"]
    assert "Sandbox Execution" in result.stdout
    assert "Python 3.12" in result.stdout
    assert "truncated" in result.stdout
