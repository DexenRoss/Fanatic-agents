"""Fanatic Agents command-line application."""

from __future__ import annotations

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

from fanatic_agents.agents._shared import (
    OpenAIConfigurationError,
    configure_openai_sdk,
)
from fanatic_agents.core.config import ConfigLoadError, ProjectConfig, load_project_config
from fanatic_agents.core.settings import ApplicationSettings, get_settings
from fanatic_agents.delivery.models import DeliveryResult
from fanatic_agents.delivery.service import deliver_promotion
from fanatic_agents.git.errors import RepositoryStateError
from fanatic_agents.git.inspection import (
    RepositoryInspectionError,
    RepositoryInspector,
    RepositorySnapshot,
)
from fanatic_agents.git.models import BaseRepositoryState, PromotionResult
from fanatic_agents.git.promotion import (
    capture_base_repository_state,
    promote_verified_changes,
)
from fanatic_agents.github.client import check_github_cli
from fanatic_agents.orchestrator.models import WorkflowResult
from fanatic_agents.implementation.models import ImplementationResult
from fanatic_agents.implementation.service import run_controlled_implementation
from fanatic_agents.observation.models import PullRequestObservation
from fanatic_agents.observation.service import (
    DEFAULT_WATCH_INTERVAL_SECONDS,
    DEFAULT_WATCH_TIMEOUT_SECONDS,
    observe_once,
    observe_until_terminal,
)
from fanatic_agents.orchestrator.workflow import run_workflow

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
workflow_app = typer.Typer(
    help="Plan, implement, deliver, and observe human-gated workflows."
)
app.add_typer(workflow_app, name="workflow")


@dataclass(frozen=True, slots=True)
class EnvironmentCheck:
    """One result displayed by the environment doctor."""

    name: str
    status: str
    ready: bool


def collect_environment_checks(
    settings: ApplicationSettings | None = None,
) -> list[EnvironmentCheck]:
    """Inspect local prerequisites without modifying local or remote state."""

    application_settings = settings or get_settings()
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
            "OK" if application_settings.has_openai_api_key else "NOT CONFIGURED",
            application_settings.has_openai_api_key,
        ),
        _github_cli_check(),
    ]


def _executable_check(name: str, executable: str) -> EnvironmentCheck:
    available = shutil.which(executable) is not None
    return EnvironmentCheck(name, "OK" if available else "NOT FOUND", available)

def _github_cli_check() -> EnvironmentCheck:
    if shutil.which("gh") is None:
        return EnvironmentCheck("GitHub CLI", "NOT FOUND", False)
    status = check_github_cli()
    if status.status == "ok":
        return EnvironmentCheck("GitHub CLI", "OK", True)
    return EnvironmentCheck("GitHub CLI", "FOUND BUT NOT AUTHENTICATED", False)



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

    settings = get_settings()
    if not settings.has_openai_api_key:
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
        configure_openai_sdk(settings)
        assessment = run_developer_assessment(snapshot)
    except (DeveloperAgentError, OpenAIConfigurationError) as exc:
        console.print("\n[red]AI analysis failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc
    _render_developer_assessment(assessment)

@workflow_app.command("plan")
def plan_workflow(
    repository: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=True,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Repository directory to plan against in read-only mode.",
    ),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Run Planner, Developer Planning, Reviewer, and QA (up to 4 calls).",
    ),
) -> None:
    """Inspect locally and optionally run one read-only workflow pass."""
    try:
        snapshot = RepositoryInspector().inspect(repository)
    except RepositoryInspectionError as exc:
        console.print("[red]Repository inspection failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc

    if not ai:
        _render_repository_snapshot(snapshot)
        console.print("\n[bold]AI workflow:[/bold] [yellow]NOT REQUESTED[/yellow]")
        console.print("Use --ai to run Planner -> Developer -> Reviewer -> QA.")
        return

    settings = get_settings()
    if not settings.has_openai_api_key:
        console.print(
            "\n[red]AI workflow requires OPENAI_API_KEY; no agent was called.[/red]"
        )
        raise typer.Exit(code=1)
    if not snapshot.has_agent_context():
        console.print(
            "\n[red]The repository snapshot is empty; no agent was called.[/red]"
        )
        raise typer.Exit(code=1)

    try:
        configure_openai_sdk(settings)
        result = run_workflow(snapshot)
    except Exception as exc:
        console.print(
            "\n[red]AI workflow failed safely; later agents were not called.[/red]"
        )
        raise typer.Exit(code=1) from exc
    _render_workflow_result(result)
    if result.status == "failed":
        raise typer.Exit(code=1)


@workflow_app.command("implement")
def implement_workflow(
    repository: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=True,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Repository protected while implementation runs in a temporary copy.",
    ),
    image: str = typer.Option(..., "--image", help="Locally available Docker image."),
    ai: bool = typer.Option(
        False,
        "--ai",
        help="Run up to five agents and verify temporary changes in Docker.",
    ),
    promote: bool = typer.Option(
        False,
        "--promote",
        help="Explicitly promote VERIFIED changes to a new local worktree.",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        help="New local fanatic/* branch required by --promote.",
    ),
) -> None:
    """Generate, verify, and optionally promote changes without touching the source tree."""
    if promote and branch is None:
        raise typer.BadParameter("--promote requires --branch.")
    if branch is not None and not promote:
        raise typer.BadParameter("--branch requires --promote.")
    if promote and not ai:
        raise typer.BadParameter("--promote requires --ai.")

    console.print("\n[bold cyan]Fanatic Agents Controlled Implementation[/bold cyan]")
    console.print("\n[bold]Original repository:[/bold] [green]PROTECTED[/green]")
    console.print("[bold]Implementation workspace:[/bold] [yellow]TEMPORARY[/yellow]")
    console.print("Changes will NOT be copied back to the repository.")
    try:
        snapshot = RepositoryInspector().inspect(repository)
    except RepositoryInspectionError as exc:
        console.print("[red]Repository inspection failed:[/red]", Text(str(exc)))
        raise typer.Exit(code=1) from exc

    console.print("\n[bold]Repository[/bold]")
    console.print(Text(snapshot.repository_name))
    if not ai:
        console.print("\n[bold]AI implementation:[/bold] [yellow]NOT REQUESTED[/yellow]")
        console.print("No agent was called and Docker was not executed.")
        return

    settings = get_settings()
    if not settings.has_openai_api_key:
        console.print(
            "\n[red]AI implementation requires OPENAI_API_KEY; "
            "no agent was called and Docker was not executed.[/red]"
        )
        raise typer.Exit(code=1)
    if not snapshot.has_agent_context():
        console.print("\n[red]The repository snapshot is empty; no agent was called.[/red]")
        raise typer.Exit(code=1)

    try:
        configure_openai_sdk(settings)
        workflow = run_workflow(snapshot)
    except Exception as exc:
        console.print("\n[red]AI workflow failed safely; implementation did not start.[/red]")
        raise typer.Exit(code=1) from exc

    console.print("\n[bold]Workflow[/bold]")
    console.print(workflow.status.upper())
    if workflow.status != "ready_for_implementation":
        if workflow.stop_reason:
            console.print(Text(workflow.stop_reason))
        console.print("\n[bold]Important:[/bold] No changes were written to the original repository.")
        raise typer.Exit(code=1)

    base_state: BaseRepositoryState | None = None
    if promote:
        try:
            base_state = capture_base_repository_state(repository)
        except RepositoryStateError as exc:
            _render_promotion_result(
                PromotionResult(
                    repository=str(Path(repository).expanduser().resolve(strict=False)),
                    promoted_branch=branch,
                    status=exc.status,
                    stop_reason=str(exc),
                ),
                implementation_status="NOT STARTED",
            )
            raise typer.Exit(code=1) from exc
        if not base_state.working_tree_clean:
            _render_promotion_result(
                PromotionResult(
                    repository=base_state.repository_path,
                    base_branch=base_state.branch,
                    base_commit=base_state.commit_sha,
                    promoted_branch=branch,
                    status="repository_dirty",
                    stop_reason="Promotion requires the original working tree to be clean.",
                ),
                implementation_status="NOT STARTED",
            )
            raise typer.Exit(code=1)

    if base_state is None:
        result = run_controlled_implementation(repository, snapshot, workflow, image)
    else:
        result = run_controlled_implementation(
            repository, snapshot, workflow, image, base_state
        )
    _render_implementation_result(result)
    if result.status != "verified":
        raise typer.Exit(code=1)
    if not promote:
        return

    assert branch is not None and workflow.developer is not None
    promotion = promote_verified_changes(
        repository,
        result,
        branch,
        workflow.developer.files_likely_affected,
    )
    _render_promotion_result(promotion)
    if promotion.status != "promoted":
        raise typer.Exit(code=1)


@workflow_app.command("deliver")
def deliver_workflow(
    promotion_worktree: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=False,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Dedicated promotion worktree created by Fanatic Agents.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Optional project YAML; its delivery permissions must be enabled.",
    ),
    commit_message: str | None = typer.Option(
        None, "--commit-message", help="Validated one-line commit subject."
    ),
    pr_title: str | None = typer.Option(
        None, "--pr-title", help="Validated one-line pull request title."
    ),
    check: bool = typer.Option(
        False, "--check", help="Run delivery preflight without staging or side effects."
    ),
) -> None:
    """Explicitly commit, push, and open a PR for one exact verified promotion."""
    project_config: ProjectConfig | None = None
    if config is not None:
        try:
            project_config = load_project_config(config)
        except (ConfigLoadError, ValidationError) as exc:
            console.print("[red]Delivery configuration is invalid:[/red]", Text(str(exc)))
            raise typer.Exit(code=1) from exc

    result = deliver_promotion(
        promotion_worktree,
        permissions=project_config.permissions if project_config else None,
        configured_repository=(
            Path(project_config.repository.path) if project_config else None
        ),
        commit_message=commit_message,
        pr_title=pr_title,
        check_only=check,
    )
    _render_delivery_result(result, check_only=check)
    if result.status not in {"ready", "delivered"}:
        raise typer.Exit(code=1)


def _render_delivery_result(
    result: DeliveryResult, *, check_only: bool = False
) -> None:
    console.print("\n[bold cyan]Fanatic Agents Git Delivery[/bold cyan]")
    console.print("\n[bold]Promotion[/bold]\nVERIFIED / PROMOTED")
    console.print(f"\n[bold]Repository[/bold]\n{Text(result.repository)}")
    console.print(f"\n[bold]Branch[/bold]\n{result.branch or 'UNAVAILABLE'}")
    if check_only and result.status == "ready":
        console.print("\n[bold]Mode[/bold]\nCHECK ONLY - NO SIDE EFFECTS")
    staging = "APPROVED" if result.commit_sha else "NOT PERFORMED"
    console.print(f"\n[bold]Staging[/bold]\n{staging}")
    console.print(f"\n[bold]Commit[/bold]\n{result.commit_sha or 'NOT CREATED'}")
    push = (
        f"{result.remote}/{result.remote_branch}"
        if result.remote and result.remote_branch
        else "NOT PERFORMED"
    )
    console.print(f"\n[bold]Push[/bold]\n{push}")
    pull_request = (
        f"#{result.pr_number} {result.pr_url}"
        if result.pr_number and result.pr_url
        else "NOT CREATED"
    )
    console.print(f"\n[bold]Pull Request[/bold]\n{pull_request}")
    console.print("\n[bold]Automatic merge[/bold]\nDISABLED")
    console.print(f"\n[bold]Final Status[/bold]\n{result.final_status}")
    if result.stop_reason:
        console.print(Text(result.stop_reason))
    console.print("\nThe original working tree was not modified.")


@workflow_app.command("observe")
def observe_workflow(
    promotion_worktree: Path = typer.Argument(
        ...,
        exists=False,
        file_okay=False,
        dir_okay=True,
        readable=False,
        resolve_path=False,
        help="Promotion worktree whose Sprint 6 delivery receipt identifies the PR.",
    ),
    config: Path | None = typer.Option(
        None,
        "--config",
        help="Optional project YAML; observe_pull_request must be enabled.",
    ),
    watch: bool = typer.Option(
        False, "--watch", help="Poll locally until a terminal state or timeout."
    ),
    interval_seconds: float = typer.Option(
        DEFAULT_WATCH_INTERVAL_SECONDS,
        "--interval-seconds",
        min=10.0,
        help="Polling interval in seconds (minimum 10).",
    ),
    timeout_seconds: float = typer.Option(
        DEFAULT_WATCH_TIMEOUT_SECONDS,
        "--timeout-seconds",
        min=1.0,
        max=1800.0,
        help="Bounded watch timeout in seconds (maximum 1800).",
    ),
) -> None:
    """Read the current CI and human review state of one delivered PR."""
    project_config: ProjectConfig | None = None
    if config is not None:
        try:
            project_config = load_project_config(config)
        except (ConfigLoadError, ValidationError) as exc:
            console.print("[red]Observation configuration is invalid:[/red]", Text(str(exc)))
            raise typer.Exit(code=1) from exc

    kwargs = {
        "permissions": project_config.permissions if project_config else None,
        "configured_repository": (
            Path(project_config.repository.path) if project_config else None
        ),
    }
    if watch:
        result = observe_until_terminal(
            promotion_worktree,
            interval_seconds=interval_seconds,
            timeout_seconds=timeout_seconds,
            **kwargs,
        )
    else:
        result = observe_once(promotion_worktree, **kwargs)
    _render_observation(result)
    if result.status in {
        "invalid_delivery",
        "github_unavailable",
        "observation_failed",
        "pr_head_drifted",
    }:
        raise typer.Exit(code=1)


def _render_observation(result: PullRequestObservation) -> None:
    console.print("\n[bold cyan]Fanatic Agents Pull Request Observation[/bold cyan]")
    console.print(f"\n[bold]Repository[/bold]\n{Text(result.repository)}")
    pull_request = (
        f"#{result.pr_number} {result.pr_url or ''}".rstrip()
        if result.pr_number
        else "UNAVAILABLE"
    )
    console.print(f"\n[bold]Pull Request[/bold]\n{pull_request}")
    branch = (
        f"{result.head_branch} -> {result.base_branch}"
        if result.head_branch and result.base_branch
        else "UNAVAILABLE"
    )
    console.print(f"\n[bold]Branch[/bold]\n{branch}")
    integrity = (
        "VERIFIED"
        if result.expected_head_sha
        and result.expected_head_sha == result.observed_head_sha
        else "DRIFTED"
        if result.observed_head_sha
        else "UNAVAILABLE"
    )
    console.print(f"\n[bold]Head integrity[/bold]\n{integrity}")
    console.print(f"\n[bold]CI[/bold]\n{result.ci_state.upper()}")
    console.print("\n[bold]Checks[/bold]")
    if not result.checks:
        console.print("NONE REPORTED")
    for check in result.checks:
        marker = (
            "✓"
            if check.conclusion in {"success", "skipped", "neutral"}
            else "✗"
            if check.conclusion in {
                "failure", "cancelled", "timed_out", "action_required"
            }
            else "…"
        )
        label = f"{check.context} / {check.name}" if check.context else check.name
        console.print(Text(f"{marker} {label}"))
    review = result.review_state.upper()
    review += f" (approvals: {result.approvals}, changes requested: {result.changes_requested})"
    console.print(f"\n[bold]Reviews[/bold]\n{review}")
    console.print(f"\n[bold]Mergeability[/bold]\n{result.mergeable.upper()}")
    console.print("\n[bold]Automatic merge[/bold]\nDISABLED")
    console.print(f"\n[bold]Final Status[/bold]\n{result.final_status}")
    if result.stop_reason:
        console.print(Text(result.stop_reason))
    console.print(
        "\nObservation was read-only; the repository and pull request were not modified."
    )


def _render_implementation_result(result: ImplementationResult) -> None:
    console.print("\n[bold]Implementation Agent[/bold]")
    count = len(result.changeset.changes) if result.changeset is not None else 0
    console.print(f"Generated changes: {count}")
    if result.changeset is not None:
        policy_status = (
            "APPROVED"
            if result.applied_changes
            else "HUMAN_REQUIRED"
            if result.status == "human_required"
            else "REJECTED"
            if result.status == "policy_rejected"
            else "NOT_APPLIED"
        )
        console.print(f"\n[bold]Policy[/bold]\n{policy_status}")
    if result.workspace_summary is not None:
        console.print("\n[bold]Temporary Workspace[/bold]\nREADY")
    if result.verification_results:
        console.print("\n[bold]Verification[/bold]")
        for verification in result.verification_results:
            console.print(Text(" ".join(verification.argv)))
            exit_code = "N/A" if verification.exit_code is None else str(verification.exit_code)
            console.print(f"Exit code: {exit_code}")
            console.print("PASS" if verification.exit_code == 0 and not verification.timed_out else "FAIL")
            _render_sandbox_stream("stdout", verification.stdout, verification.stdout_truncated)
            _render_sandbox_stream("stderr", verification.stderr, verification.stderr_truncated)
    console.print("\n[bold]Final Status[/bold]")
    console.print(result.status.upper())
    if result.stop_reason:
        console.print(Text(result.stop_reason))
    console.print("\n[bold]Important:[/bold] No changes were written to the original repository.")


def _render_promotion_result(
    result: PromotionResult, *, implementation_status: str = "VERIFIED"
) -> None:
    console.print("\n[bold cyan]Fanatic Agents Verified Change Promotion[/bold cyan]")
    console.print("\n[bold]Original repository[/bold]")
    console.print(Text(result.repository))
    console.print("[green]PROTECTED[/green]")
    console.print(f"\n[bold]Implementation[/bold]\n{implementation_status}")
    promotion_status = "APPROVED" if result.status == "promoted" else "REJECTED"
    console.print(f"\n[bold]Promotion[/bold]\n{promotion_status}")
    console.print(f"\n[bold]Branch[/bold]\n{result.promoted_branch or 'NOT CREATED'}")
    console.print(f"\n[bold]Worktree[/bold]\n{result.worktree_path or 'NOT CREATED'}")
    console.print(f"\n[bold]Changes[/bold]\n{result.changes}")
    console.print("\n[bold]Commit[/bold]\nNOT CREATED")
    console.print("\n[bold]Push[/bold]\nNOT PERFORMED")
    console.print(f"\n[bold]Final Status[/bold]\n{result.status.upper()}")
    if result.stop_reason:
        console.print(Text(result.stop_reason))
    console.print("\nThe original working tree was not modified.")
    if result.status == "promoted":
        console.print(
            "Changes are available for human review in the promotion worktree."
        )

def _render_workflow_result(result: WorkflowResult) -> None:
    console.print("\n[bold cyan]Fanatic Agents Workflow[/bold cyan]")
    console.print("\n[bold]Repository[/bold]")
    console.print(Text(result.repository.repository_name))

    if result.planner is not None:
        console.print("\n[bold]Planner[/bold]")
        if result.planner.selected_task is not None:
            task = result.planner.selected_task
            console.print("Task: ", Text(task.title))
            console.print(f"Risk: {task.risk_level}")
        else:
            console.print("No task selected (insufficient context).")

    if result.developer is not None:
        console.print("\n[bold]Developer Plan[/bold]")
        console.print("Approach: ", Text(result.developer.approach))
        _render_list("Files", result.developer.files_likely_affected)

    if result.reviewer is not None:
        console.print("\n[bold]Reviewer[/bold]")
        console.print(f"Decision: {result.reviewer.decision}")

    if result.qa is not None:
        console.print("\n[bold]QA[/bold]")
        console.print(f"Readiness: {result.qa.readiness}")

    console.print("\n[bold]Final Status[/bold]")
    console.print(result.status.upper())
    if result.stop_reason:
        console.print("\n[bold]Stop reason:[/bold]")
        console.print(Text(result.stop_reason))



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

