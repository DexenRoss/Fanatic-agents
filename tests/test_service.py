from __future__ import annotations

import inspect
import json
import signal
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click import unstyle
from pydantic import ValidationError
from typer.testing import CliRunner

from fanatic_agents.autonomous.models import AutonomousRunResult
from fanatic_agents.cli.main import app
from fanatic_agents.core.config import ProjectConfig, ServiceConfig
from fanatic_agents.core.settings import ApplicationSettings
from fanatic_agents.scheduler.models import SchedulerRunResult
from fanatic_agents.scheduler.service import SchedulerService
from fanatic_agents.scheduler.state import SchedulerStateStore
from fanatic_agents.service import cli as service_cli
from fanatic_agents.service import manager as manager_module
from fanatic_agents.service import systemd as systemd_module
from fanatic_agents.service.manager import (
    ManagedServiceError,
    ManagedServiceManager,
    render_unit,
    service_name_for,
    sha256_file,
)
from fanatic_agents.service.models import PlatformCheck
from fanatic_agents.service.receipt import (
    ManagedServiceReceiptStore,
    ServiceReceiptError,
)
from fanatic_agents.service.systemd import SystemdUserError, SystemdUserManager

NOW = datetime(2026, 8, 31, tzinfo=UTC)
SECRET = "super-secret-test-key"


class FakeGit:
    def __init__(self, repository: Path, identity: str = "owner/repo") -> None:
        self.repository = repository.resolve()
        self.identity = identity
        self.calls: list[tuple[str, ...]] = []

    def run(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments == ("rev-parse", "--is-inside-work-tree"):
            value = "true\n"
        elif arguments == ("rev-parse", "--show-toplevel"):
            value = f"{self.repository}\n"
        elif arguments == ("remote", "get-url", "origin"):
            value = f"https://github.com/{self.identity}.git\n"
        else:
            return subprocess.CompletedProcess(["git", *arguments], 1, "", "")
        return subprocess.CompletedProcess(["git", *arguments], 0, value, "")


class FakeSystemd:
    def __init__(self, unit_directory: Path) -> None:
        self.unit_directory = unit_directory.resolve()
        self.calls: list[tuple[str, ...]] = []
        self.active = False
        self.enabled = False

    def check(self) -> PlatformCheck:
        self.calls.append(("check",))
        return PlatformCheck(
            platform="Linux",
            systemd_available=True,
            systemctl_available=True,
            user_manager_reachable=True,
            supported=True,
            wsl_detected=False,
        )

    def unit_path(self, name: str) -> Path:
        return self.unit_directory / name

    def write_unit(self, path: Path, content: str, *, replace: bool) -> None:
        self.calls.append(("write", path.name, str(replace)))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def remove_unit(self, path: Path) -> None:
        self.calls.append(("remove", path.name))
        path.unlink()

    def daemon_reload(self) -> None:
        self.calls.append(("daemon-reload",))

    def enable(self, name: str) -> None:
        self.calls.append(("enable", name))
        self.enabled = True

    def disable(self, name: str) -> None:
        self.calls.append(("disable", name))
        self.enabled = False

    def start(self, name: str) -> None:
        self.calls.append(("start", name))
        self.active = True

    def stop(self, name: str) -> None:
        self.calls.append(("stop", name))
        self.active = False

    def is_enabled(self, name: str) -> bool:
        self.calls.append(("is-enabled", name))
        return self.enabled

    def is_active(self, name: str) -> bool:
        self.calls.append(("is-active", name))
        return self.active

    def active_state(self, name: str) -> str:
        self.calls.append(("is-active", name))
        return "active" if self.active else "inactive"


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run_forever(self, config: ProjectConfig, **kwargs: object) -> SchedulerRunResult:
        self.calls.append({"config": config, **kwargs})
        return SchedulerRunResult(
            repository=str(kwargs["repository"]),
            status="stopped_by_user",
        )


def config_data(repository: Path, *, service_enabled: bool = True) -> dict[str, object]:
    return {
        "project": {"name": "Service Test"},
        "repository": {"path": str(repository), "main_branch": "main"},
        "commands": {"setup": [], "test": [], "build": []},
        "limits": {
            "max_tasks_per_day": 1,
            "max_runtime_minutes": 10,
            "max_daily_cost_usd": 1.0,
            "max_iterations_per_task": 1,
        },
        "permissions": {
            "read_issues": True,
            "autonomous_execution": True,
            "observe_pull_request": True,
        },
        "intake": {"enabled": True},
        "autonomy": {"enabled": True},
        "scheduler": {"enabled": True},
        "service": {"enabled": service_enabled, "manager": "systemd_user"},
    }


def setup_manager(
    tmp_path: Path, *, service_enabled: bool = True
) -> tuple[ManagedServiceManager, FakeSystemd, FakeGit, Path, Path, Path]:
    repository = tmp_path / "repo"
    repository.mkdir()
    config = tmp_path / "project.yaml"
    config.write_text(
        yaml.safe_dump(config_data(repository, service_enabled=service_enabled)),
        encoding="utf-8",
    )
    executable = tmp_path / "fanatic-agents"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    systemd = FakeSystemd(tmp_path / "units")
    git = FakeGit(repository)
    manager = ManagedServiceManager(
        systemd=systemd,  # type: ignore[arg-type]
        receipts=ManagedServiceReceiptStore(metadata_root=tmp_path / "metadata"),
        git=git,  # type: ignore[arg-type]
        clock=lambda: NOW,
    )
    return manager, systemd, git, repository, config, executable


def install(manager: ManagedServiceManager, repository: Path, config: Path, executable: Path, **kwargs: object):
    return manager.install(
        repository,
        config_path=config,
        image="python:3.12-slim",
        executable=executable,
        **kwargs,
    )


def test_service_config_is_strict_independent_and_disabled() -> None:
    assert ServiceConfig() == ServiceConfig(enabled=False, manager="systemd_user")
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate({"unexpected": True})
    with pytest.raises(ValidationError):
        ServiceConfig.model_validate({"manager": "cron"})
    config = ProjectConfig.model_validate(config_data(Path("/repo")))
    assert config.service.enabled is True
    assert config.scheduler.enabled is True
    config = ProjectConfig.model_validate(
        {**config_data(Path("/repo")), "service": {"enabled": False}}
    )
    assert config.scheduler.enabled is True
    assert config.service.enabled is False


@pytest.mark.parametrize(
    ("systemd_available", "systemctl", "returncode", "supported"),
    [
        (False, None, 0, False),
        (True, None, 0, False),
        (True, "/usr/bin/systemctl", 1, False),
        (True, "/usr/bin/systemctl", 0, True),
    ],
)
def test_platform_check_is_read_only_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    systemd_available: bool,
    systemctl: str | None,
    returncode: int,
    supported: bool,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, returncode, "", "")

    monkeypatch.setattr(systemd_module.sys, "platform", "linux")
    monkeypatch.setattr(
        systemd_module, "_systemd_available", lambda: systemd_available
    )
    monkeypatch.setattr(systemd_module.shutil, "which", lambda _name: systemctl)
    result = SystemdUserManager(
        runner=runner, unit_directory=tmp_path
    ).check()
    assert result.supported is supported
    assert all(call[0][1:] == ["--user", "status"] for call in calls)
    assert all(call[1]["shell"] is False for call in calls)


def test_systemctl_uses_exact_argv_and_never_shell(tmp_path: Path) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    manager = SystemdUserManager(
        runner=runner, systemctl="/usr/bin/systemctl", unit_directory=tmp_path
    )
    name = "fanatic-agents-repo-0123456789ab.service"
    manager.daemon_reload()
    manager.enable(name)
    manager.start(name)
    manager.stop(name)
    manager.disable(name)
    assert [item[0] for item in calls] == [
        ["/usr/bin/systemctl", "--user", "daemon-reload"],
        ["/usr/bin/systemctl", "--user", "enable", name],
        ["/usr/bin/systemctl", "--user", "start", name],
        ["/usr/bin/systemctl", "--user", "stop", name],
        ["/usr/bin/systemctl", "--user", "disable", name],
    ]
    assert all(item[1]["shell"] is False for item in calls)


def test_systemctl_failure_reports_only_bounded_safe_reason(tmp_path: Path) -> None:
    name = "fanatic-agents-repo-0123456789ab.service"
    reason = f"Failed to start {name}: Unit has a bad unit file setting."

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", reason + "\n" + "x" * 500)

    manager = SystemdUserManager(
        runner=runner,
        systemctl="/usr/bin/systemctl",
        unit_directory=tmp_path,
    )
    with pytest.raises(SystemdUserError) as error:
        manager.start(name)
    assert reason in str(error.value)
    assert "x" * 500 not in str(error.value)


def test_systemctl_failure_does_not_expose_secret_bearing_output(
    tmp_path: Path,
) -> None:
    name = "fanatic-agents-repo-0123456789ab.service"

    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv,
            1,
            "",
            f"Failed to start {name}: OPENAI_API_KEY={SECRET}\n",
        )

    manager = SystemdUserManager(
        runner=runner,
        systemctl="/usr/bin/systemctl",
        unit_directory=tmp_path,
    )
    with pytest.raises(SystemdUserError) as error:
        manager.start(name)
    assert str(error.value) == "systemctl rejected the managed user-service operation."
    assert SECRET not in str(error.value)


def test_unit_writer_refuses_symlink_targets(tmp_path: Path) -> None:
    name = "fanatic-agents-repo-0123456789ab.service"
    foreign = tmp_path / "foreign"
    foreign.write_text("foreign", encoding="utf-8")
    path = tmp_path / name
    path.symlink_to(foreign)
    manager = SystemdUserManager(unit_directory=tmp_path)
    with pytest.raises(SystemdUserError, match="unsafe"):
        manager.write_unit(path, "unit", replace=True)


def test_failed_systemd_state_is_reported(tmp_path: Path) -> None:
    def runner(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 3, "failed\n", "")

    manager = SystemdUserManager(
        runner=runner,
        systemctl="/usr/bin/systemctl",
        unit_directory=tmp_path,
    )
    name = "fanatic-agents-repo-0123456789ab.service"
    assert manager.active_state(name) == "failed"


def test_render_unit_uses_systemd_syntax_not_shell_quoting() -> None:
    repository = Path("/home/dexen/Documentos/fanatic-agents-lab")
    executable = Path("/opt/fanatic-agents/bin/fanatic-agents")
    receipt = Path("/home/dexen/.local/state/fanatic-agents/receipt.json")

    unit = render_unit(
        description="Lab scheduler",
        repository=repository,
        executable=executable,
        receipt_path=receipt,
    )

    assert (
        "WorkingDirectory=/home/dexen/Documentos/fanatic-agents-lab\n" in unit
    )
    assert 'WorkingDirectory="/home/dexen/Documentos/fanatic-agents-lab"' not in unit
    assert (
        "ExecStart=/opt/fanatic-agents/bin/fanatic-agents service run-managed "
        "/home/dexen/.local/state/fanatic-agents/receipt.json\n" in unit
    )
    assert "Restart=no" in unit
    assert SECRET not in unit
    assert "bash -c" not in unit and "sh -c" not in unit
    source = inspect.getsource(manager_module)
    assert "shlex" not in source and "shell=True" not in source


def test_render_unit_preserves_working_directory_with_spaces() -> None:
    repository = Path("/home/dexen/Documentos/fanatic agents lab")
    unit = render_unit(
        description="Lab scheduler",
        repository=repository,
        executable=Path("/opt/Fanatic Agents/bin/fanatic-agents"),
        receipt_path=Path("/home/dexen/.local/state/fanatic agents/receipt.json"),
    )

    working_directory = next(
        line.removeprefix("WorkingDirectory=")
        for line in unit.splitlines()
        if line.startswith("WorkingDirectory=")
    )
    assert working_directory == str(repository)
    assert Path(working_directory).is_absolute()
    assert (
        'ExecStart="/opt/Fanatic Agents/bin/fanatic-agents" service run-managed '
        '"/home/dexen/.local/state/fanatic agents/receipt.json"' in unit
    )


@pytest.mark.parametrize("unsafe", ["/repo\nnext", "/repo\rnext", "/repo\0next"])
def test_render_unit_rejects_unrepresentable_values(unsafe: str) -> None:
    with pytest.raises(ManagedServiceError, match="invalid characters"):
        render_unit(
            description="Lab scheduler",
            repository=Path(unsafe),
            executable=Path("/opt/fanatic-agents"),
            receipt_path=Path("/var/lib/fanatic-agents/receipt.json"),
        )


def test_install_defaults_are_safe_and_secret_free(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    env_file.chmod(0o600)
    receipt = install(
        manager, repository, config, executable, env_file=env_file
    )
    unit = Path(receipt.unit_path).read_text(encoding="utf-8")
    receipt_text = manager._receipts.path_for(repository).read_text(encoding="utf-8")
    assert "Restart=no" in unit
    assert f"ExecStart={executable.resolve()}" in unit
    assert "run-managed" in unit
    assert SECRET not in unit
    assert SECRET not in receipt_text
    assert str(env_file.resolve()) in receipt_text
    assert receipt.config_sha256 == sha256_file(config)
    assert receipt.deliver_authorized is False
    assert receipt.enabled_at is None and receipt.started_at is None
    assert not any(call[0] in {"enable", "start"} for call in systemd.calls)
    assert not Path(receipt.unit_path).is_relative_to(repository)
    assert not manager._receipts.path_for(repository).is_relative_to(repository)


def test_install_requires_all_independent_gates(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(
        tmp_path, service_enabled=False
    )
    with pytest.raises(ManagedServiceError, match="service.enabled"):
        install(manager, repository, config, executable)
    assert systemd.calls == [("check",)]


def test_install_enable_start_and_delivery_are_explicit(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(tmp_path)
    data = config_data(repository)
    data["autonomy"] = {
        "enabled": True,
        "auto_promote": True,
        "auto_deliver": True,
    }
    permissions = data["permissions"]
    assert isinstance(permissions, dict)
    permissions.update(
        commit=True, push_branch=True, create_pull_request=True
    )
    config.write_text(yaml.safe_dump(data), encoding="utf-8")
    receipt = install(
        manager,
        repository,
        config,
        executable,
        deliver=True,
        enable=True,
        start=True,
    )
    assert receipt.deliver_authorized is True
    assert ("enable", receipt.service_name) in systemd.calls
    assert ("start", receipt.service_name) in systemd.calls


def test_service_name_is_deterministic_and_avoids_basename_collisions(
    tmp_path: Path,
) -> None:
    one = tmp_path / "one" / "repo"
    two = tmp_path / "two" / "repo"
    one.mkdir(parents=True)
    two.mkdir(parents=True)
    assert service_name_for(one) == service_name_for(one)
    assert service_name_for(one) != service_name_for(two)


def test_duplicate_install_requires_explicit_replace(tmp_path: Path) -> None:
    manager, _systemd, _git, repository, config, executable = setup_manager(tmp_path)
    first = install(manager, repository, config, executable)
    with pytest.raises(ManagedServiceError, match="--replace"):
        install(manager, repository, config, executable)
    second = install(manager, repository, config, executable, replace=True)
    assert second.service_name == first.service_name


def test_config_and_unit_drift_fail_closed(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(tmp_path)
    receipt = install(manager, repository, config, executable)
    config.write_text(config.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(ManagedServiceError, match="CONFIG_DRIFTED"):
        manager.start(repository)
    assert not any(call[0] == "start" for call in systemd.calls)
    config.write_text(yaml.safe_dump(config_data(repository)), encoding="utf-8")
    receipt = install(manager, repository, config, executable, replace=True)
    Path(receipt.unit_path).write_text("tampered", encoding="utf-8")
    with pytest.raises(ManagedServiceError, match="UNIT_DRIFTED"):
        manager.start(repository)


def test_repository_identity_drift_fails_closed(tmp_path: Path) -> None:
    manager, _systemd, git, repository, config, executable = setup_manager(tmp_path)
    install(manager, repository, config, executable)
    git.identity = "other/repo"
    with pytest.raises(ManagedServiceError, match="identity drifted"):
        manager.start(repository)


def test_corrupt_receipt_is_preserved(tmp_path: Path) -> None:
    manager, _systemd, _git, repository, config, executable = setup_manager(tmp_path)
    install(manager, repository, config, executable)
    path = manager._receipts.path_for(repository)
    path.write_text("{bad", encoding="utf-8")
    with pytest.raises(ServiceReceiptError, match="preserved"):
        manager.start(repository)
    assert path.read_text(encoding="utf-8") == "{bad"


def test_start_stop_status_use_only_exact_unit(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(tmp_path)
    receipt = install(manager, repository, config, executable)
    manager.start(repository)
    manager.stop(repository)
    status = manager.status(repository)
    assert status.installed is True
    assert status.config_drift is False
    assert status.unit_drift is False
    assert ("start", receipt.service_name) in systemd.calls
    assert ("stop", receipt.service_name) in systemd.calls
    assert all(
        len(call) < 2 or call[1] == receipt.service_name
        for call in systemd.calls
        if call[0] in {"start", "stop", "is-active", "is-enabled"}
    )


def test_uninstall_removes_only_service_artifacts(tmp_path: Path) -> None:
    manager, systemd, _git, repository, config, executable = setup_manager(tmp_path)
    receipt = install(manager, repository, config, executable)
    systemd.active = True
    systemd.enabled = True
    unrelated = tmp_path / "metadata" / "scheduler-receipt.json"
    unrelated.parent.mkdir(parents=True, exist_ok=True)
    unrelated.write_text("keep", encoding="utf-8")
    manager.uninstall(repository)
    assert not Path(receipt.unit_path).exists()
    assert not manager._receipts.path_for(repository).exists()
    assert unrelated.read_text(encoding="utf-8") == "keep"
    assert ("stop", receipt.service_name) in systemd.calls
    assert ("disable", receipt.service_name) in systemd.calls


def test_env_file_permissions_are_conservative(tmp_path: Path) -> None:
    manager, _systemd, _git, repository, config, executable = setup_manager(tmp_path)
    env_file = tmp_path / ".env"
    env_file.write_text(f"OPENAI_API_KEY={SECRET}\n", encoding="utf-8")
    env_file.chmod(0o604)
    with pytest.raises(ManagedServiceError, match="world-readable"):
        install(
            manager, repository, config, executable, env_file=env_file
        )
    assert SECRET not in repr(manager)


def test_run_managed_validates_then_reuses_scheduler(tmp_path: Path) -> None:
    manager, systemd, git, repository, config, executable = setup_manager(tmp_path)
    scheduler = FakeScheduler()
    manager = ManagedServiceManager(
        systemd=systemd,  # type: ignore[arg-type]
        receipts=manager._receipts,
        git=git,  # type: ignore[arg-type]
        scheduler_factory=lambda: scheduler,  # type: ignore[arg-type]
        settings_loader=lambda **_kwargs: ApplicationSettings(
            OPENAI_API_KEY=SECRET, _env_file=None
        ),
        provider_configurer=lambda _settings: True,
        clock=lambda: NOW,
    )
    receipt = install(manager, repository, config, executable)
    result = manager.run_managed(manager._receipts.path_for(repository))
    assert result.status == "stopped_by_user"
    assert scheduler.calls[0]["repository"] == repository.resolve()
    assert scheduler.calls[0]["deliver"] is False


def test_receipt_model_and_outputs_never_expose_secret(tmp_path: Path) -> None:
    manager, _systemd, _git, repository, config, executable = setup_manager(tmp_path)
    receipt = install(manager, repository, config, executable)
    assert SECRET not in receipt.model_dump_json()
    assert SECRET not in repr(receipt)
    source = inspect.getsource(ManagedServiceManager)
    assert "shell=True" not in source
    assert "bash -c" not in source
    assert "sh -c" not in source


def test_service_cli_help_surface_is_stable() -> None:
    runner = CliRunner()
    commands = [
        ["service", "--help"],
        ["service", "check", "--help"],
        ["service", "install", "--help"],
        ["service", "start", "--help"],
        ["service", "stop", "--help"],
        ["service", "status", "--help"],
        ["service", "uninstall", "--help"],
    ]
    for command in commands:
        result = runner.invoke(app, command)
        assert result.exit_code == 0, result.stdout
        assert "Usage" in unstyle(result.stdout)


def test_managed_sigterm_gracefully_stops_and_allows_immediate_restart(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base, systemd, git, repository, config, executable = setup_manager(tmp_path)
    metadata = tmp_path / "scheduler-metadata"
    states = SchedulerStateStore(metadata_root=metadata)
    signal_changes: list[tuple[signal.Signals, object]] = []
    current_handler: dict[str, object] = {"value": signal.SIG_IGN}
    lock_observations: list[bool] = []

    class NoTaskRunner:
        def __init__(self) -> None:
            self.calls = 0

        def run_once(
            self, _config: ProjectConfig, **kwargs: object
        ) -> AutonomousRunResult:
            self.calls += 1
            managed_repository = Path(str(kwargs["repository"]))
            return AutonomousRunResult(
                repository=str(managed_repository),
                github_repository="owner/repo",
                started_at=NOW,
                finished_at=NOW,
                status="no_eligible_tasks",
            )

    runner = NoTaskRunner()

    def get_signal(_signum: signal.Signals) -> object:
        return current_handler["value"]

    def set_signal(signum: signal.Signals, handler: object) -> object:
        previous = current_handler["value"]
        current_handler["value"] = handler
        signal_changes.append((signum, handler))
        return previous

    def interrupt_managed_sleep(_seconds: float) -> None:
        lock_observations.append(states.lock_path(repository).exists())
        handler = current_handler["value"]
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(manager_module.signal, "getsignal", get_signal)
    monkeypatch.setattr(manager_module.signal, "signal", set_signal)
    scheduler = SchedulerService(
        runner=runner,  # type: ignore[arg-type]
        metadata_root=metadata,
        sleeper=interrupt_managed_sleep,
        clock=lambda: NOW,
    )
    manager = ManagedServiceManager(
        systemd=systemd,  # type: ignore[arg-type]
        receipts=base._receipts,
        git=git,  # type: ignore[arg-type]
        scheduler_factory=lambda: scheduler,
        settings_loader=lambda **_kwargs: ApplicationSettings(
            OPENAI_API_KEY=SECRET, _env_file=None
        ),
        provider_configurer=lambda _settings: True,
        clock=lambda: NOW,
    )
    receipt = install(manager, repository, config, executable)
    receipt_path = manager._receipts.path_for(repository)
    assert receipt_path == manager._receipts.path_for(Path(receipt.repository))
    monkeypatch.setattr(service_cli, "ManagedServiceManager", lambda: manager)
    cli = CliRunner()

    for _attempt in range(2):
        result = cli.invoke(
            app, ["service", "run-managed", str(receipt_path)]
        )
        assert result.exit_code == 0, result.stdout
        assert "Final status: STOPPED_BY_USER" in unstyle(result.stdout)
        assert not states.lock_path(repository).exists()
        assert states.load(repository).last_result_status == "stopped_by_user"

    assert runner.calls == 2
    assert lock_observations == [True, True]
    assert current_handler["value"] is signal.SIG_IGN
    assert len(signal_changes) == 4
    assert all(signum is signal.SIGTERM for signum, _handler in signal_changes)
    assert signal_changes[1][1] is signal.SIG_IGN
    assert signal_changes[3][1] is signal.SIG_IGN


def test_managed_run_does_not_install_signal_handler_from_worker_thread(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    base, systemd, git, repository, config, executable = setup_manager(tmp_path)
    scheduler = FakeScheduler()
    manager = ManagedServiceManager(
        systemd=systemd,  # type: ignore[arg-type]
        receipts=base._receipts,
        git=git,  # type: ignore[arg-type]
        scheduler_factory=lambda: scheduler,  # type: ignore[arg-type]
        settings_loader=lambda **_kwargs: ApplicationSettings(
            OPENAI_API_KEY=SECRET, _env_file=None
        ),
        provider_configurer=lambda _settings: True,
        clock=lambda: NOW,
    )
    install(manager, repository, config, executable)
    receipt_path = manager._receipts.path_for(repository)
    signal_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        manager_module.signal,
        "getsignal",
        lambda *args: signal_calls.append(args),
    )
    monkeypatch.setattr(
        manager_module.signal,
        "signal",
        lambda *args: signal_calls.append(args),
    )
    results: list[SchedulerRunResult] = []
    errors: list[BaseException] = []

    def run() -> None:
        try:
            results.append(manager.run_managed(receipt_path))
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert errors == []
    assert [result.status for result in results] == ["stopped_by_user"]
    assert signal_calls == []


def test_start_and_stop_preserve_scheduler_lock_and_signal_handlers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager, _systemd, _git, repository, config, executable = setup_manager(tmp_path)
    install(manager, repository, config, executable)
    states = SchedulerStateStore(metadata_root=tmp_path / "metadata")
    lock_path = states.lock_path(repository)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("preserve-existing-lock", encoding="utf-8")
    signal_calls: list[tuple[object, ...]] = []
    monkeypatch.setattr(
        manager_module.signal,
        "signal",
        lambda *args: signal_calls.append(args),
    )
    monkeypatch.setattr(service_cli, "ManagedServiceManager", lambda: manager)
    cli = CliRunner()

    start_result = cli.invoke(app, ["service", "start", str(repository)])
    stop_result = cli.invoke(app, ["service", "stop", str(repository)])

    assert start_result.exit_code == 0, start_result.stdout
    assert stop_result.exit_code == 0, stop_result.stdout
    assert lock_path.read_text(encoding="utf-8") == "preserve-existing-lock"
    assert signal_calls == []
