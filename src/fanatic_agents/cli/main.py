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

from fanatic_agents.core.config import ConfigLoadError, load_project_config


app = typer.Typer(
    name="fanatic-agents",
    help="Safe foundations for orchestrating software engineering agents.",
    no_args_is_help=True,
)
config_app = typer.Typer(help="Validate and inspect project configuration.")
app.add_typer(config_app, name="config")
console = Console()


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


if __name__ == "__main__":
    app()

