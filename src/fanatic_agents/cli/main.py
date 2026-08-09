"""Fanatic Agents command-line application."""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from fanatic_agents.agents.developer import (
    DeveloperAgentError,
    DeveloperAssessment,
    run_developer_assessment,
)

from fanatic_agents.core.config import ConfigLoadError, load_project_config
from fanatic_agents.git.inspection import (
    RepositoryInspectionError,
    RepositoryInspector,
    RepositorySnapshot,
)

from fanatic_agents.sandbox.docker import (
    check_docker_sandbox,
    run_sandbox_command,
)
from fanatic_agents.sandbox.errors import SandboxError
from fanatic_agents.sandbox.models import SandboxCommandResult
from fanatic_agents.sandbox.policy import parse_command

app = typer.Typer(
    name="fanatic-agents",
    help="Safe foundations for orchestrating software engineering agents.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Validate and inspect project configuration.")
app.add_typer(config_app, name="config")
console = Console()
sandbox_app = typer.Typer(help="Check and run the experimental Docker sandbox.")
app.add_typer(sandbox_app, name="sandbox")


@dataclass(frozen=True, slots=True)
class EnvironmentCheck:
    """One result displayed by the environment doctor."""

    name: str
    status: str
    ready: bool


def collect_environment_checks() -> list[EnvironmentCheck]:
    """Inspect local prerequisites without invoking tools or network services."""

    python_version = ".".join(str(part) for part in sys.version_info[:3])
    python_ready = sys.version_info >= (3, 12)
    return [
        EnvironmentCheck(
            "Python",
            f"OK ({python_version})" if python_ready else f"UNSUPPORTED ({python_version})",
            python_ready,
        ),
        _executable_check("Git", "git"),
        _executable_check("Docker", "docker"),
        EnvironmentCheck(
            "OpenAI API Key",
            "OK" if os.environ.get("OPENAI_API_KEY") else "NOT CONFIGURED",
            bool(os.environ.get("OPENAI_API_KEY")),
        ),
        _executable_check("GitHub CLI", "gh"),
    ]


def _executable_check(name: str, executable: str) -> EnvironmentCheck:
    available = shutil.which(executable) is not None
    return EnvironmentCheck(name, "OK" if available else "NOT FOUND", available)


@app.command()
def doctor() -> None:
    """Report whether local development prerequisites are available."""

    checks = collect_environment_checks()
    table = Table(title="Fanatic Agents Environment", show_header=False)
    table.add_column("Component", style="bold")
    table.add_column("Status")

    for check in checks:
        style = "green" if check.ready else "yellow"
        table.add_row(check.name, f"[{style}]{check.status}[/{style}]")

    console.print(table)
    environment_status = "READY" if all(check.ready for check in checks) else "PARTIALLY READY"
    style = "green" if environment_status == "READY" else "yellow"
    console.print(f"\nEnvironment: [{style}]{environment_status}[/{style}]")


@config_app.command("validate")
def validate_config(
    file: Path = typer.Argument(
        ...,
        exists=False,
        dir_okay=False,
        readable=False,
        resolve_path=False,
        help="Path to a project YAML file.",
    ),
) -> None:
    """Load and strictly validate a project YAML file."""

    try:
        project_config = load_project_config(file)
    except ConfigLoadError as exc:
        console.print(f"[red]Configuration could not be loaded:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except ValidationError as exc:
        console.print(f"[red]Configuration is invalid:[/red] {file}")
        for error in exc.errors(include_url=False):
            location = ".".join(str(part) for part in error["loc"])
            console.print(f"  [bold]{location}[/bold]: {error['msg']}")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]Configuration is valid:[/green] {file} "
        f"([bold]{project_config.project.name}[/bold])"
    )


@sandbox_app.command("check")
def sandbox_check() -> None:
    """Check the Docker CLI and daemon without pulling any image."""
    try:
        status = check_docker_sandbox()
    except SandboxError as exc:
        table = Table(title="Sandbox Check", show_header=False)
        table.add_column("Component", style="bold")
        table.add_column("Status")
        table.add_row("Docker", "[red]UNAVAILABLE[/red]")
        console.print(table)
        console.print("\n[red]Sandbox is not ready:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc

    table = Table(title="Sandbox Check", show_header=False)
    table.add_column("Component", style="bold")
    table.add_column("Status")
    table.add_row("Docker CLI", Text(status.executable))
    table.add_row("Docker daemon", "[green]AVAILABLE[/green]")
    console.print(table)
    console.print("\n[green]Sandbox preflight passed.[/green]")


@sandbox_app.command("run")
def sandbox_run(
    repository: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=True,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Repository copied into the temporary sandbox workspace.",
    ),
    image: str = typer.Option(..., "--image", help="Locally available Docker image."),
    command_text: str = typer.Option(
        ...,
        "--command",
        help="Command parsed once into argv; shell syntax is rejected.",
    ),
) -> None:
    """Run one allowed command in an isolated copy of a repository."""
    try:
        command = parse_command(command_text)
        result = run_sandbox_command(repository, image, command)
    except SandboxError as exc:
        console.print("[red]Sandbox execution failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc
    _render_sandbox_result(image, result)


def _render_sandbox_result(image: str, result: SandboxCommandResult) -> None:
    table = Table(title="Sandbox Execution", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    table.add_row("Image", Text(image))
    table.add_row("Command", Text(" ".join(result.argv)))
    exit_code = str(result.exit_code) if result.exit_code is not None else "N/A"
    table.add_row("Exit code", exit_code)
    table.add_row("Duration", f"{result.duration_seconds:.3f}s")
    table.add_row("Timed out", "YES" if result.timed_out else "NO")
    console.print(table)
    _render_sandbox_stream("stdout", result.stdout, result.stdout_truncated)
    _render_sandbox_stream("stderr", result.stderr, result.stderr_truncated)


def _render_sandbox_stream(title: str, value: str, truncated: bool) -> None:
    suffix = " [yellow](truncated)[/yellow]" if truncated else ""
    console.print(f"\n[bold]{title}[/bold]{suffix}")
    console.print(Text(value) if value else Text("(empty)"))


@app.command("inspect")
def inspect_repository(
    repository: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=True,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Repository directory to inspect in read-only mode.",
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Request one read-only Developer Agent assessment (may incur API cost).",
    ),
) -> None:
    """Inspect a repository locally, with optional bounded AI analysis."""
    try:
        snapshot = RepositoryInspector().inspect(repository)
    except RepositoryInspectionError as exc:
        console.print("[red]Repository inspection failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc

    _render_repository_snapshot(snapshot)
    if not ai:
        console.print("\n[bold]AI Analysis[/bold]        [yellow]NOT REQUESTED[/yellow]")
        return

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        console.print(
            "\n[red]AI analysis requires OPENAI_API_KEY; no API request was made.[/red]"
        )
        raise typer.Exit(code=1)
    if not snapshot.has_agent_context():
        console.print(
            "\n[red]The repository snapshot is empty; no API request was made.[/red]"
        )
        raise typer.Exit(code=1)

    try:
        assessment = run_developer_assessment(snapshot)
    except DeveloperAgentError as exc:
        console.print("\n[red]AI analysis failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc
    _render_developer_assessment(assessment)


def _render_repository_snapshot(snapshot: RepositorySnapshot) -> None:
    table = Table(title="Fanatic Agents Repository Inspection", show_header=False)
    table.add_column("Property", style="bold")
    table.add_column("Value")
    branch = "DETACHED HEAD" if snapshot.detached_head else snapshot.current_branch or "N/A"
    working_tree = (
        "N/A"
        if snapshot.working_tree_clean is None
        else "CLEAN" if snapshot.working_tree_clean else "DIRTY"
    )
    table.add_row("Repository", Text(snapshot.repository_name))
    table.add_row("Git", "YES" if snapshot.is_git_repository else "NO")
    table.add_row("Branch", branch)
    table.add_row("Working tree", working_tree)
    console.print(table)
    _render_list("Detected technologies", snapshot.detected_technologies)
    _render_list("Important files", snapshot.important_files)
    _render_list("Testing commands", snapshot.inferred_test_commands)
    _render_list("Build commands", snapshot.inferred_build_commands)
    truncation = snapshot.truncation
    if truncation.relevant_files_omitted or truncation.content_files_omitted:
        console.print(
            "\n[yellow]Snapshot bounded:[/yellow] "
            f"{truncation.relevant_files_omitted} paths and "
            f"{truncation.content_files_omitted} content files omitted."
        )


def _render_developer_assessment(assessment: DeveloperAssessment) -> None:
    console.print("\n[bold cyan]Developer Agent Assessment[/bold cyan]")
    console.print("\n[bold]Summary[/bold]")
    console.print(Text(assessment.summary))
    console.print("\n[bold]Architecture[/bold]")
    console.print(Text(assessment.architecture))
    _render_list("Key components", assessment.key_components)
    _render_list("Risks", assessment.risks)
    _render_list("Recommended tasks", assessment.recommended_tasks)
    _render_list("Testing notes", assessment.testing_notes)
    console.print(f"\n[bold]Readiness[/bold]\n{assessment.readiness}")


def _render_list(title: str, items: list[str]) -> None:
    console.print(f"\n[bold]{title}[/bold]")
    if not items:
        console.print("- None detected")
        return
    for item in items:
        console.print(Text(f"- {item}"))

if __name__ == "__main__":
    app()

