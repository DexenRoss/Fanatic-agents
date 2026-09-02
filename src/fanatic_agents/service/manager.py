"""Reusable orchestration for an explicitly managed scheduler service."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

from pydantic import ValidationError

from fanatic_agents.agents._shared import OpenAIConfigurationError, configure_openai_sdk
from fanatic_agents.core.config import (
    ConfigLoadError,
    ProjectConfig,
    load_project_config,
)
from fanatic_agents.core.settings import ApplicationSettings, get_settings
from fanatic_agents.git.errors import GitCommandError
from fanatic_agents.git.worktree import GitRunner
from fanatic_agents.github.client import parse_github_repository
from fanatic_agents.scheduler.models import SchedulerRunResult
from fanatic_agents.scheduler.service import SchedulerService
from fanatic_agents.scheduler.state import SchedulerStateError, SchedulerStateStore
from fanatic_agents.service.models import (
    ManagedServiceReceipt,
    ManagedServiceStatus,
    PlatformCheck,
)
from fanatic_agents.service.receipt import (
    ManagedServiceReceiptStore,
    ServiceReceiptError,
)
from fanatic_agents.service.systemd import SystemdUserError, SystemdUserManager


class ManagedServiceError(RuntimeError):
    """A managed-service operation failed closed."""


class ManagedServiceManager:
    """Own installation lifecycle and the tamper-checked runtime boundary."""

    def __init__(
        self,
        *,
        systemd: SystemdUserManager | None = None,
        receipts: ManagedServiceReceiptStore | None = None,
        git: GitRunner | None = None,
        scheduler_factory: Callable[[], SchedulerService] = SchedulerService,
        settings_loader: Callable[..., ApplicationSettings] = get_settings,
        provider_configurer: Callable[[ApplicationSettings], bool] = configure_openai_sdk,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._systemd = systemd or SystemdUserManager()
        self._receipts = receipts or ManagedServiceReceiptStore()
        self._git = git or GitRunner()
        self._scheduler_factory = scheduler_factory
        self._settings_loader = settings_loader
        self._provider_configurer = provider_configurer
        self._clock = clock or (lambda: datetime.now(UTC))

    def check(self) -> PlatformCheck:
        return self._systemd.check()

    def install(
        self,
        repository: Path,
        *,
        config_path: Path,
        image: str,
        executable: Path,
        env_file: Path | None = None,
        deliver: bool = False,
        enable: bool = False,
        start: bool = False,
        replace: bool = False,
    ) -> ManagedServiceReceipt:
        check = self.check()
        if not check.supported:
            raise ManagedServiceError(check.reason or "Managed services are unsupported.")
        resolved = _regular_directory(repository, "repository")
        config_file = _regular_file(config_path, "configuration")
        executable_file = _executable_file(executable)
        env_path = _safe_env_file(env_file) if env_file is not None else None
        config = _load_config(config_file)
        _authorize(config, deliver=deliver)
        if Path(config.repository.path).expanduser().resolve(strict=True) != resolved:
            raise ManagedServiceError(
                "The configured repository path does not match the requested repository."
            )
        github_repository = self._repository_identity(resolved)
        service_name = service_name_for(resolved)
        unit_path = self._systemd.unit_path(service_name)
        receipt_path = self._receipts.path_for(resolved)
        previous: ManagedServiceReceipt | None = None
        if receipt_path.exists():
            previous = self._receipts.load(resolved)
            if not replace:
                raise ManagedServiceError(
                    "A managed service already exists; use --replace explicitly."
                )
            self._validate_owned_unit(previous)
            if self._systemd.is_active(previous.service_name):
                raise ManagedServiceError(
                    "Stop the active managed service before replacing it."
                )
            if Path(previous.unit_path) != unit_path:
                raise ManagedServiceError("Existing managed service identity is ambiguous.")
        elif replace:
            raise ManagedServiceError("No managed service exists to replace.")
        elif unit_path.exists() or unit_path.is_symlink():
            raise ManagedServiceError(
                "The deterministic unit name is already owned by another service."
            )

        content = render_unit(
            description=config.project.name,
            repository=resolved,
            executable=executable_file,
            receipt_path=receipt_path,
        )
        now = self._clock()
        receipt = ManagedServiceReceipt(
            service_name=service_name,
            repository=str(resolved),
            github_repository=github_repository,
            main_branch=config.repository.main_branch,
            config_path=str(config_file),
            config_sha256=sha256_file(config_file),
            image=image,
            deliver_authorized=deliver,
            executable=str(executable_file),
            env_file_path=str(env_path) if env_path is not None else None,
            unit_path=str(unit_path),
            unit_sha256=sha256_bytes(content.encode("utf-8")),
            installed_at=previous.installed_at if previous is not None else now,
            enabled_at=previous.enabled_at if previous is not None else None,
            started_at=previous.started_at if previous is not None else None,
            updated_at=now,
        )
        self._systemd.write_unit(unit_path, content, replace=replace)
        try:
            self._receipts.save(receipt, replace=replace)
        except Exception:
            if previous is None:
                try:
                    self._systemd.remove_unit(unit_path)
                except SystemdUserError:
                    pass
            raise
        self._systemd.daemon_reload()
        if enable:
            self._systemd.enable(service_name)
            receipt = self._update(receipt, enabled_at=self._clock())
        if start:
            self._systemd.start(service_name)
            receipt = self._update(receipt, started_at=self._clock())
        return receipt

    def start(self, repository: Path) -> ManagedServiceReceipt:
        receipt, _ = self._validate_runtime(repository)
        self._systemd.start(receipt.service_name)
        return self._update(receipt, started_at=self._clock())

    def stop(self, repository: Path) -> ManagedServiceReceipt:
        receipt = self._receipts.load(repository)
        self._validate_repository(receipt)
        self._validate_owned_unit(receipt)
        self._systemd.stop(receipt.service_name)
        return receipt

    def status(self, repository: Path) -> ManagedServiceStatus:
        requested = Path(repository).expanduser().resolve(strict=True)
        if not self._receipts.path_for(requested).exists():
            return ManagedServiceStatus(installed=False, repository=str(requested))
        receipt = self._receipts.load(requested)
        self._validate_repository(receipt)
        config_drift = _hash_drift(Path(receipt.config_path), receipt.config_sha256)
        unit_drift = _hash_drift(Path(receipt.unit_path), receipt.unit_sha256)
        scheduler_state = None
        try:
            scheduler_state = SchedulerStateStore().load(requested).last_result_status
        except SchedulerStateError:
            pass
        active_state = getattr(self._systemd, "active_state", None)
        systemd_state = (
            active_state(receipt.service_name)
            if callable(active_state)
            else (
                "active"
                if self._systemd.is_active(receipt.service_name)
                else "inactive"
            )
        )
        return ManagedServiceStatus(
            installed=True,
            enabled=self._systemd.is_enabled(receipt.service_name),
            active=systemd_state == "active",
            systemd_state=systemd_state,
            service_name=receipt.service_name,
            manager=receipt.manager,
            repository=receipt.repository,
            config_drift=config_drift,
            unit_drift=unit_drift,
            scheduler_state=scheduler_state,
        )

    def uninstall(self, repository: Path) -> ManagedServiceReceipt:
        receipt = self._receipts.load(repository)
        self._validate_repository(receipt)
        self._validate_owned_unit(receipt)
        if self._systemd.is_active(receipt.service_name):
            self._systemd.stop(receipt.service_name)
        if self._systemd.is_enabled(receipt.service_name):
            self._systemd.disable(receipt.service_name)
        self._systemd.remove_unit(Path(receipt.unit_path))
        self._systemd.daemon_reload()
        self._receipts.delete(receipt)
        return receipt

    def run_managed(self, receipt_path: Path) -> SchedulerRunResult:
        receipt = self._receipts.load_path(receipt_path)
        receipt, config = self._validate_runtime(Path(receipt.repository), receipt=receipt)
        settings = self._load_settings(receipt)
        if not settings.has_openai_api_key:
            raise ManagedServiceError(
                "Managed scheduler settings are incomplete; no task was selected."
            )
        try:
            self._provider_configurer(settings)
        except OpenAIConfigurationError as exc:
            raise ManagedServiceError(
                "Managed scheduler provider setup failed safely."
            ) from exc
        with _managed_sigterm_as_keyboard_interrupt():
            return self._scheduler_factory().run_forever(
                config,
                image=receipt.image,
                repository=Path(receipt.repository),
                deliver=receipt.deliver_authorized,
            )

    def _validate_runtime(
        self,
        repository: Path,
        *,
        receipt: ManagedServiceReceipt | None = None,
    ) -> tuple[ManagedServiceReceipt, ProjectConfig]:
        current = receipt or self._receipts.load(repository)
        self._validate_repository(current)
        self._validate_owned_unit(current)
        config_path = _regular_file(Path(current.config_path), "configuration")
        if sha256_file(config_path) != current.config_sha256:
            raise ManagedServiceError("CONFIG_DRIFTED")
        config = _load_config(config_path)
        _authorize(config, deliver=current.deliver_authorized)
        resolved = Path(current.repository).resolve(strict=True)
        if (
            Path(config.repository.path).expanduser().resolve(strict=True) != resolved
            or config.repository.main_branch != current.main_branch
        ):
            raise ManagedServiceError("Configured repository identity drifted.")
        _executable_file(Path(current.executable))
        if current.env_file_path is not None:
            _safe_env_file(Path(current.env_file_path))
        return current, config

    def _validate_repository(self, receipt: ManagedServiceReceipt) -> None:
        repository = _regular_directory(Path(receipt.repository), "repository")
        if self._receipts.path_for(repository) != self._receipts.path_for(
            Path(receipt.repository)
        ):
            raise ManagedServiceError("Managed repository path drifted.")
        if self._repository_identity(repository).casefold() != (
            receipt.github_repository.casefold()
        ):
            raise ManagedServiceError("Managed repository identity drifted.")

    def _repository_identity(self, repository: Path) -> str:
        try:
            inside = self._git.run(repository, "rev-parse", "--is-inside-work-tree")
            top = self._git.run(repository, "rev-parse", "--show-toplevel")
            remote = self._git.run(repository, "remote", "get-url", "origin")
        except GitCommandError as exc:
            raise ManagedServiceError("Git repository identity is unavailable.") from exc
        if (
            inside.returncode != 0
            or inside.stdout.strip() != "true"
            or top.returncode != 0
            or remote.returncode != 0
        ):
            raise ManagedServiceError("Git repository identity is unavailable.")
        try:
            root = Path(top.stdout.strip()).resolve(strict=True)
        except OSError as exc:
            raise ManagedServiceError("Git repository identity is unavailable.") from exc
        github = parse_github_repository(remote.stdout.strip())
        if root != repository or github is None:
            raise ManagedServiceError(
                "A repository-root path with a supported GitHub origin is required."
            )
        return github

    def _validate_owned_unit(self, receipt: ManagedServiceReceipt) -> None:
        expected = self._systemd.unit_path(receipt.service_name)
        if Path(receipt.unit_path) != expected:
            raise ManagedServiceError("Managed unit path is invalid.")
        if _hash_drift(expected, receipt.unit_sha256):
            raise ManagedServiceError("UNIT_DRIFTED")

    def _load_settings(self, receipt: ManagedServiceReceipt) -> ApplicationSettings:
        try:
            if receipt.env_file_path is None:
                return self._settings_loader()
            return self._settings_loader(env_file=Path(receipt.env_file_path))
        except (OSError, ValidationError, ValueError) as exc:
            raise ManagedServiceError(
                "Managed scheduler settings could not be loaded safely."
            ) from exc

    def _update(
        self, receipt: ManagedServiceReceipt, **changes: datetime
    ) -> ManagedServiceReceipt:
        updated = receipt.model_copy(
            update={**changes, "updated_at": self._clock()}
        )
        self._receipts.save(updated, replace=True)
        return updated


def service_name_for(repository: Path) -> str:
    resolved = Path(repository).expanduser().resolve(strict=True)
    slug = re.sub(r"[^a-z0-9]+", "-", resolved.name.casefold()).strip("-")
    slug = (slug or "repository")[:48].strip("-")
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()[:12]
    return f"fanatic-agents-{slug}-{digest}.service"


def render_unit(
    *,
    description: str,
    repository: Path,
    executable: Path,
    receipt_path: Path,
) -> str:
    if not repository.is_absolute():
        raise ManagedServiceError("Managed service repository path must be absolute.")
    if not executable.is_absolute() or not receipt_path.is_absolute():
        raise ManagedServiceError("Managed unit command paths must be absolute.")
    _validate_systemd_value(description)
    label = " ".join(description.split()).replace("\\", "-")[:120]
    command = " ".join(
        _systemd_exec_argument(str(value))
        for value in (executable, "service", "run-managed", receipt_path)
    )
    return (
        "[Unit]\n"
        "Description="
        f"{_systemd_directive_value(f'Fanatic Agents scheduler for {label}')}\n"
        "After=default.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"WorkingDirectory={_systemd_directive_value(str(repository))}\n"
        f"ExecStart={command}\n"
        "Restart=no\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def detect_executable() -> Path:
    candidate = shutil.which("fanatic-agents")
    if candidate is None:
        candidate = str(Path(sys.executable).resolve().parent / "fanatic-agents")
    return _executable_file(Path(candidate))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ManagedServiceError("Managed file could not be hashed safely.") from exc
    return digest.hexdigest()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hash_drift(path: Path, expected: str) -> bool:
    try:
        return (
            path.is_symlink()
            or not path.is_file()
            or sha256_file(path) != expected
        )
    except (ManagedServiceError, OSError):
        return True


def _load_config(path: Path) -> ProjectConfig:
    try:
        return load_project_config(path)
    except (ConfigLoadError, ValidationError) as exc:
        raise ManagedServiceError("Managed project configuration is invalid.") from exc


def _authorize(config: ProjectConfig, *, deliver: bool) -> None:
    gates = {
        "service.enabled": config.service.enabled,
        "scheduler.enabled": config.scheduler.enabled,
        "autonomy.enabled": config.autonomy.enabled,
        "intake.enabled": config.intake.enabled,
        "permissions.read_issues": config.permissions.read_issues,
        "permissions.autonomous_execution": config.permissions.autonomous_execution,
    }
    denied = [name for name, value in gates.items() if not value]
    if denied:
        raise ManagedServiceError(
            "Managed service authorization is denied: " + ", ".join(denied)
        )
    if deliver:
        delivery_gates = {
            "autonomy.auto_deliver": config.autonomy.auto_deliver,
            "permissions.commit": config.permissions.commit,
            "permissions.push_branch": config.permissions.push_branch,
            "permissions.create_pull_request": config.permissions.create_pull_request,
        }
        denied = [name for name, value in delivery_gates.items() if not value]
        if denied:
            raise ManagedServiceError(
                "Persistent delivery authorization is denied: " + ", ".join(denied)
            )


def _regular_directory(path: Path, label: str) -> Path:
    expanded = Path(path).expanduser()
    if expanded.is_symlink() or not expanded.is_dir():
        raise ManagedServiceError(f"The {label} must be a real directory.")
    try:
        return expanded.resolve(strict=True)
    except OSError as exc:
        raise ManagedServiceError(f"The {label} path is invalid.") from exc


def _regular_file(path: Path, label: str) -> Path:
    expanded = Path(path).expanduser()
    if expanded.is_symlink() or not expanded.is_file():
        raise ManagedServiceError(f"The {label} must be a regular non-symlink file.")
    try:
        return expanded.resolve(strict=True)
    except OSError as exc:
        raise ManagedServiceError(f"The {label} path is invalid.") from exc


def _executable_file(path: Path) -> Path:
    try:
        resolved = Path(path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ManagedServiceError("The Fanatic Agents executable is invalid.") from exc
    if (
        not resolved.is_absolute()
        or not resolved.is_file()
        or not os.access(resolved, os.X_OK)
    ):
        raise ManagedServiceError(
            "An absolute executable Fanatic Agents path is required."
        )
    return resolved


def _safe_env_file(path: Path) -> Path:
    resolved = _regular_file(path, "environment file")
    try:
        details = resolved.stat()
    except OSError as exc:
        raise ManagedServiceError(
            "The environment file could not be inspected."
        ) from exc
    if hasattr(os, "getuid") and details.st_uid != os.getuid():
        raise ManagedServiceError(
            "The environment file must be owned by the current user."
        )
    if stat.S_IMODE(details.st_mode) & stat.S_IROTH:
        raise ManagedServiceError(
            "The environment file must not be world-readable."
        )
    return resolved


@contextmanager
def _managed_sigterm_as_keyboard_interrupt() -> Iterator[None]:
    """Give only a main-thread managed run the scheduler's graceful stop path."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    previous = signal.getsignal(signal.SIGTERM)

    def stop_gracefully(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_gracefully)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def _systemd_directive_value(value: str) -> str:
    """Serialize one scalar unit value without applying argv or shell quoting."""
    _validate_systemd_value(value)
    if not value or value != value.strip() or "\\" in value:
        raise ManagedServiceError("Managed unit value cannot be represented safely.")
    return value.replace("%", "%%")


def _systemd_exec_argument(value: str) -> str:
    """Serialize one argument according to systemd's ExecStart parser."""
    _validate_systemd_value(value)
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
        .replace("$", "$$")
    )
    if not value or any(character.isspace() for character in value) or any(
        character in value for character in ('"', "'", "\\")
    ):
        return f'"{escaped}"'
    return escaped


def _validate_systemd_value(value: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ManagedServiceError("Managed unit values contain invalid characters.")
