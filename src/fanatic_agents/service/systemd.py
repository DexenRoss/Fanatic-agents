"""Narrow, argv-only boundary for systemd user services."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from fanatic_agents.service.models import PlatformCheck
from fanatic_agents.service.receipt import ServiceReceiptError, atomic_write

UNIT_NAME = re.compile(r"^fanatic-agents-[a-z0-9-]+-[0-9a-f]{12}\.service$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SAFE_SYSTEMCTL_REASON = re.compile(
    r"^(?:Failed to |Job for |Unit |Failed to connect to bus:)", re.IGNORECASE
)
SENSITIVE_OUTPUT = re.compile(
    r"(?:api[_-]?key|authorization|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
COMMAND_TIMEOUT_SECONDS = 20.0
MAX_FAILURE_REASON_LENGTH = 240


class SystemdUserError(RuntimeError):
    """The systemd user manager operation failed safely."""


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


class SystemdUserManager:
    """Run only the bounded systemctl --user operations used by Sprint 11."""

    def __init__(
        self,
        *,
        runner: RunCommand = subprocess.run,
        systemctl: str | None = None,
        unit_directory: Path | None = None,
    ) -> None:
        self._runner = runner
        self._systemctl = systemctl
        xdg = os.environ.get("XDG_CONFIG_HOME")
        self.unit_directory = (
            Path(unit_directory).expanduser().resolve(strict=False)
            if unit_directory is not None
            else (
                Path(xdg).expanduser() if xdg else Path.home() / ".config"
            ).resolve(strict=False)
            / "systemd"
            / "user"
        )

    def check(self) -> PlatformCheck:
        """Check availability without changing service or system state."""
        name = platform.system() or sys.platform
        wsl = _detect_wsl() if sys.platform.startswith("linux") else False
        systemd = _systemd_available()
        executable = self._systemctl or shutil.which("systemctl")
        if not sys.platform.startswith("linux"):
            return PlatformCheck(
                platform=name,
                systemd_available=False,
                systemctl_available=executable is not None,
                user_manager_reachable=False,
                supported=False,
                wsl_detected=wsl,
                reason="Only Linux/WSL systemd user services are supported.",
            )
        if not systemd or executable is None:
            return PlatformCheck(
                platform=name,
                systemd_available=systemd,
                systemctl_available=executable is not None,
                user_manager_reachable=False,
                supported=False,
                wsl_detected=wsl,
                reason="systemd and systemctl are required.",
            )
        result = self._invoke([executable, "--user", "status"], check_result=False)
        reachable = result.returncode == 0
        return PlatformCheck(
            platform=name,
            systemd_available=True,
            systemctl_available=True,
            user_manager_reachable=reachable,
            supported=reachable,
            wsl_detected=wsl,
            reason=None if reachable else "The systemd user manager is not reachable.",
        )

    def unit_path(self, service_name: str) -> Path:
        _validate_unit_name(service_name)
        return self.unit_directory / service_name

    def write_unit(self, path: Path, content: str, *, replace: bool) -> None:
        target = Path(path).expanduser()
        if (
            not target.is_absolute()
            or target.parent.resolve(strict=False) != self.unit_directory
            or target != self.unit_path(target.name)
        ):
            raise SystemdUserError("Managed unit path is outside the user unit directory.")
        if target.is_symlink():
            raise SystemdUserError("The target systemd user unit is unsafe.")
        if target.exists() and not replace:
            raise SystemdUserError("The target systemd user unit already exists.")
        if target.exists() and not target.is_file():
            raise SystemdUserError("The target systemd user unit is unsafe.")
        try:
            atomic_write(target, content.encode("utf-8"), 0o644)
        except ServiceReceiptError as exc:
            raise SystemdUserError("The systemd user unit could not be written safely.") from exc

    def remove_unit(self, path: Path) -> None:
        target = Path(path).expanduser()
        if (
            not target.is_absolute()
            or target.parent.resolve(strict=False) != self.unit_directory
            or target != self.unit_path(target.name)
        ):
            raise SystemdUserError("Managed unit path is outside the user unit directory.")
        if target.is_symlink() or not target.is_file():
            raise SystemdUserError("The managed systemd user unit is unsafe.")
        try:
            target.unlink()
        except OSError as exc:
            raise SystemdUserError("The managed systemd user unit could not be removed.") from exc

    def daemon_reload(self) -> None:
        self._command("daemon-reload")

    def enable(self, service_name: str) -> None:
        self._unit_command("enable", service_name)

    def disable(self, service_name: str) -> None:
        self._unit_command("disable", service_name)

    def start(self, service_name: str) -> None:
        self._unit_command("start", service_name)

    def stop(self, service_name: str) -> None:
        self._unit_command("stop", service_name)

    def is_enabled(self, service_name: str) -> bool:
        result = self._unit_command("is-enabled", service_name, check_result=False)
        if result.returncode not in {0, 1}:
            raise SystemdUserError("The managed unit enabled state is ambiguous.")
        return result.returncode == 0

    def is_active(self, service_name: str) -> bool:
        return self.active_state(service_name) == "active"

    def active_state(self, service_name: str) -> str:
        result = self._unit_command("is-active", service_name, check_result=False)
        state = result.stdout.strip().casefold()
        if result.returncode not in {0, 3}:
            raise SystemdUserError("The managed unit active state is ambiguous.")
        if not state:
            raise SystemdUserError("The managed unit active state is empty.")
        return state

    def _unit_command(
        self, operation: str, service_name: str, *, check_result: bool = True
    ) -> subprocess.CompletedProcess[str]:
        _validate_unit_name(service_name)
        return self._command(operation, service_name, check_result=check_result)

    def _command(
        self, *arguments: str, check_result: bool = True
    ) -> subprocess.CompletedProcess[str]:
        executable = self._systemctl or shutil.which("systemctl")
        if executable is None:
            raise SystemdUserError("systemctl is not available.")
        return self._invoke(
            [executable, "--user", *arguments], check_result=check_result
        )

    def _invoke(
        self, argv: Sequence[str], *, check_result: bool
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._runner(
                list(argv),
                capture_output=True,
                check=False,
                shell=False,
                text=True,
                timeout=COMMAND_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
            raise SystemdUserError("systemctl could not complete safely.") from exc
        if check_result and result.returncode != 0:
            reason = _safe_failure_reason(result)
            if reason is not None:
                raise SystemdUserError(
                    "systemctl rejected the managed user-service operation. "
                    f"Reason: {reason}"
                )
            raise SystemdUserError("systemctl rejected the managed user-service operation.")
        return result


def _validate_unit_name(service_name: str) -> None:
    if not UNIT_NAME.fullmatch(service_name):
        raise SystemdUserError("Managed systemd unit name is invalid.")


def _safe_failure_reason(result: subprocess.CompletedProcess[str]) -> str | None:
    """Return one bounded, low-risk systemctl diagnostic line when available."""
    for output in (result.stderr, result.stdout):
        for line in output.splitlines():
            reason = " ".join(ANSI_ESCAPE.sub("", line).split())
            if not reason or not SAFE_SYSTEMCTL_REASON.match(reason):
                continue
            if SENSITIVE_OUTPUT.search(reason):
                return None
            return reason[:MAX_FAILURE_REASON_LENGTH]
    return None


def _systemd_available() -> bool:
    return Path("/run/systemd/system").is_dir()


def _detect_wsl() -> bool | None:
    try:
        values = [
            Path("/proc/sys/kernel/osrelease").read_text(encoding="utf-8"),
            Path("/proc/version").read_text(encoding="utf-8"),
        ]
    except OSError:
        return None
    text = " ".join(values).casefold()
    return "microsoft" in text or "wsl" in text
