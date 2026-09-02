"""Typer commands for explicit managed-service lifecycle operations."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from fanatic_agents.service.manager import (
    ManagedServiceError,
    ManagedServiceManager,
    detect_executable,
)
from fanatic_agents.service.models import ManagedServiceReceipt, ManagedServiceStatus
from fanatic_agents.service.receipt import ServiceReceiptError
from fanatic_agents.service.systemd import SystemdUserError

service_app = typer.Typer(
    help="Manage one explicit systemd user service for a safe scheduler."
)
console = Console()
SERVICE_ERRORS = (
    ManagedServiceError,
    ServiceReceiptError,
    SystemdUserError,
    OSError,
    ValueError,
)


@service_app.command("check")
def check_command() -> None:
    """Read-only check for a reachable systemd user manager."""
    try:
        result = ManagedServiceManager().check()
    except SERVICE_ERRORS as exc:
        _failure("Service platform check failed", exc)
    table = Table(title="Managed Service Platform", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Platform", Text(result.platform))
    table.add_row("systemd available", _yes_no(result.systemd_available))
    table.add_row("systemctl available", _yes_no(result.systemctl_available))
    table.add_row("User manager reachable", _yes_no(result.user_manager_reachable))
    table.add_row(
        "WSL detected",
        "UNKNOWN" if result.wsl_detected is None else _yes_no(result.wsl_detected),
    )
    table.add_row("Supported", _yes_no(result.supported))
    console.print(table)
    if result.reason:
        console.print(Text(result.reason))
    if not result.supported:
        raise typer.Exit(code=1)


@service_app.command("install")
def install_command(
    project: Path = typer.Argument(
        ..., exists=False, file_okay=False, help="Local GitHub repository to manage."
    ),
    config: Path = typer.Option(
        ..., "--config", help="YAML with explicit service authorization."
    ),
    image: str = typer.Option(..., "--image", help="Docker verification image."),
    env_file: Path | None = typer.Option(
        None, "--env-file", help="Private dotenv path; contents are not stored."
    ),
    deliver: bool = typer.Option(
        False, "--deliver", help="Persist separate delivery consent after all gates."
    ),
    enable: bool = typer.Option(
        False, "--enable", help="Explicitly enable the systemd user unit."
    ),
    start: bool = typer.Option(
        False, "--start", help="Explicitly start the installed unit."
    ),
    replace: bool = typer.Option(
        False, "--replace", help="Explicitly replace only the owned service."
    ),
) -> None:
    """Install an exact unit; do not enable or start by default."""
    try:
        result = ManagedServiceManager().install(
            project,
            config_path=config,
            image=image,
            executable=detect_executable(),
            env_file=env_file,
            deliver=deliver,
            enable=enable,
            start=start,
            replace=replace,
        )
    except SERVICE_ERRORS as exc:
        _failure("Service installation failed safely", exc)
    _render_receipt(result)


@service_app.command("start")
def start_command(
    project: Path = typer.Argument(..., exists=False, file_okay=False),
) -> None:
    """Validate every managed identity and start only the exact owned unit."""
    try:
        result = ManagedServiceManager().start(project)
    except SERVICE_ERRORS as exc:
        _failure("Service start failed safely", exc)
    console.print(f"[green]Started managed service:[/green] {result.service_name}")


@service_app.command("stop")
def stop_command(
    project: Path = typer.Argument(..., exists=False, file_okay=False),
) -> None:
    """Stop only the exact owned unit without changing task lifecycle state."""
    try:
        result = ManagedServiceManager().stop(project)
    except SERVICE_ERRORS as exc:
        _failure("Service stop failed safely", exc)
    console.print(f"[yellow]Stopped managed service:[/yellow] {result.service_name}")


@service_app.command("status")
def status_command(
    project: Path = typer.Argument(..., exists=False, file_okay=False),
) -> None:
    """Report installation, systemd, drift, and scheduler state read-only."""
    try:
        result = ManagedServiceManager().status(project)
    except SERVICE_ERRORS as exc:
        _failure("Service status failed safely", exc)
    _render_status(result)


@service_app.command("uninstall")
def uninstall_command(
    project: Path = typer.Argument(..., exists=False, file_okay=False),
) -> None:
    """Remove only the owned unit and its service receipt."""
    try:
        result = ManagedServiceManager().uninstall(project)
    except SERVICE_ERRORS as exc:
        _failure("Service uninstall failed safely", exc)
    console.print(f"[green]Uninstalled managed service:[/green] {result.service_name}")


@service_app.command("run-managed", hidden=True)
def run_managed_command(
    receipt: Path = typer.Argument(..., exists=False, dir_okay=False),
) -> None:
    """Run only after every receipt identity and hash validates."""
    try:
        result = ManagedServiceManager().run_managed(receipt)
    except SERVICE_ERRORS as exc:
        _failure("Managed scheduler refused to start", exc)
    console.print("[bold cyan]Fanatic Agents Managed Scheduler[/bold cyan]")
    console.print(f"Repository: {Text(result.repository)}")
    console.print(f"Cycles executed: {result.cycles_executed}")
    console.print(f"Final status: {result.final_status}")
    if result.stop_reason:
        console.print(Text(result.stop_reason))
    if result.status != "stopped_by_user":
        raise typer.Exit(code=1)


def _failure(title: str, exc: BaseException) -> NoReturn:
    console.print(f"[red]{title}.[/red]")
    if str(exc):
        console.print(Text(str(exc)))
    raise typer.Exit(code=1) from exc


def _render_receipt(result: ManagedServiceReceipt) -> None:
    table = Table(title="Managed Service Installed", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Service", Text(result.service_name))
    table.add_row("Repository", Text(result.repository))
    table.add_row("Manager", result.manager)
    table.add_row("Delivery authorized", _yes_no(result.deliver_authorized))
    table.add_row("Enabled by install", _yes_no(result.enabled_at is not None))
    table.add_row("Started by install", _yes_no(result.started_at is not None))
    console.print(table)


def _render_status(result: ManagedServiceStatus) -> None:
    table = Table(title="Managed Service Status", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Installed", _yes_no(result.installed))
    table.add_row("Repository", Text(result.repository))
    if result.installed:
        table.add_row("Service", Text(result.service_name or "UNKNOWN"))
        table.add_row("Manager", result.manager or "UNKNOWN")
        table.add_row("Enabled", _optional_yes_no(result.enabled))
        table.add_row("Active", _optional_yes_no(result.active))
        table.add_row("systemd state", result.systemd_state or "UNKNOWN")
        table.add_row("Config drift", _optional_yes_no(result.config_drift))
        table.add_row("Unit drift", _optional_yes_no(result.unit_drift))
        table.add_row("Scheduler state", result.scheduler_state or "NOT RECORDED")
    console.print(table)


def _yes_no(value: bool) -> str:
    return "YES" if value else "NO"


def _optional_yes_no(value: bool | None) -> str:
    return "UNKNOWN" if value is None else _yes_no(value)
