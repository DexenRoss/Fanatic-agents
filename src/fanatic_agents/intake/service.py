"""Read-only GitHub Issue discovery and local deterministic selection."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fanatic_agents.core.config import IntakeConfig, PermissionsConfig
from fanatic_agents.git.errors import GitCommandError
from fanatic_agents.git.worktree import GitRunner
from fanatic_agents.github.client import (
    GitHubCli,
    GitHubCommandError,
    GitHubPreflight,
    parse_github_repository,
)
from fanatic_agents.intake.models import (
    CandidateAssessment,
    GitHubIssueCandidate,
    IssueParseError,
    TaskDiscoveryResult,
    TaskIntakeReceipt,
    TaskIntakeResult,
    TaskSpec,
    parse_github_issue,
)
from fanatic_agents.intake.policy import TaskIntakePolicy
from fanatic_agents.intake.receipt import (
    TaskIntakeReceiptError,
    TaskIntakeReceiptStore,
)

COMMIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


class GitHubIssueClient(Protocol):
    def preflight(self) -> GitHubPreflight: ...

    def list_open_issues(
        self, repository: str, *, limit: int
    ) -> list[dict[str, object]]: ...


class IntakeFailure(RuntimeError):
    """A safe terminal intake failure carrying a structured result status."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class RepositoryContext:
    """Validated local and GitHub repository identity."""

    def __init__(
        self,
        *,
        repository: Path,
        github_repository: str,
        branch: str,
        head_sha: str,
    ) -> None:
        self.repository = repository
        self.github_repository = github_repository
        self.branch = branch
        self.head_sha = head_sha


class TaskIntakeService:
    """Discover and reserve work without invoking agents or mutating Git/GitHub."""

    def __init__(
        self,
        *,
        git: GitRunner | None = None,
        github: GitHubIssueClient | None = None,
        receipts: TaskIntakeReceiptStore | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._git = git or GitRunner()
        self._github = github or GitHubCli()
        self._receipts = receipts or TaskIntakeReceiptStore()
        self._clock = clock or (lambda: datetime.now(UTC))

    def discover(
        self,
        repository: Path,
        *,
        intake_config: IntakeConfig | None = None,
        permissions: PermissionsConfig | None = None,
        configured_repository: Path | None = None,
    ) -> TaskDiscoveryResult:
        requested = _requested_path(repository)
        denied = _authorization_failure(intake_config, permissions)
        if denied is not None:
            return TaskDiscoveryResult(
                repository=requested,
                status=denied[0],
                stop_reason=denied[1],
            )
        config = intake_config or IntakeConfig(enabled=True)
        try:
            context = self._resolve_repository(repository, configured_repository)
            issues, invalid = self._fetch(context, config.max_candidates)
            active = self._receipts.active_issue_numbers(
                context.repository, context.github_repository
            )
            assessments = [
                *(
                    TaskIntakePolicy(config).evaluate(
                        issue, active_issue_numbers=active
                    )
                    for issue in issues
                ),
                *invalid,
            ]
        except IntakeFailure as exc:
            return TaskDiscoveryResult(
                repository=requested,
                status=exc.status,
                stop_reason=str(exc),
            )
        except TaskIntakeReceiptError as exc:
            return TaskDiscoveryResult(
                repository=requested,
                status="intake_failed",
                stop_reason=str(exc),
            )

        eligible = [
            assessment.issue
            for assessment in TaskIntakePolicy.rank(assessments)
            if assessment.issue is not None
        ]
        return TaskDiscoveryResult(
            repository=str(context.repository),
            github_repository=context.github_repository,
            candidates_fetched=len(issues) + len(invalid),
            candidates_eligible=len(eligible),
            eligible_candidates=eligible,
            assessments=assessments,
            status="tasks_discovered" if eligible else "no_eligible_tasks",
            stop_reason=None if eligible else "No eligible GitHub Issues were found.",
        )

    def select(
        self,
        repository: Path,
        *,
        intake_config: IntakeConfig | None = None,
        permissions: PermissionsConfig | None = None,
        configured_repository: Path | None = None,
    ) -> TaskIntakeResult:
        requested = _requested_path(repository)
        denied = _authorization_failure(intake_config, permissions)
        if denied is not None:
            return TaskIntakeResult(
                repository=requested,
                status=denied[0],
                stop_reason=denied[1],
            )
        config = intake_config or IntakeConfig(enabled=True)
        try:
            context = self._resolve_repository(repository, configured_repository)
            issues, invalid = self._fetch(context, config.max_candidates)
            with self._receipts.lock(context.repository):
                active = self._receipts.active_issue_numbers(
                    context.repository, context.github_repository
                )
                policy = TaskIntakePolicy(config)
                assessments = [
                    *(
                        policy.evaluate(issue, active_issue_numbers=active)
                        for issue in issues
                    ),
                    *invalid,
                ]
                ranked = policy.rank(assessments)
                if not ranked:
                    ambiguous = any(
                        item.reason == "ambiguous_priority" for item in assessments
                    )
                    return TaskIntakeResult(
                        repository=str(context.repository),
                        github_repository=context.github_repository,
                        candidates_fetched=len(issues) + len(invalid),
                        candidates_eligible=0,
                        status=(
                            "ambiguous_priority"
                            if ambiguous
                            else "no_eligible_tasks"
                        ),
                        stop_reason=(
                            "No Issue could be selected because priority labels are ambiguous."
                            if ambiguous
                            else "No eligible GitHub Issues were found."
                        ),
                    )
                selected = ranked[0]
                assert selected.issue is not None
                selected_at = self._clock()
                task = _task_spec(context, selected, selected_at)
                receipt = _receipt(context, selected, selected_at)
                receipt_path = self._receipts.save(receipt)
        except IntakeFailure as exc:
            return TaskIntakeResult(
                repository=requested,
                status=exc.status,
                stop_reason=str(exc),
            )
        except TaskIntakeReceiptError as exc:
            return TaskIntakeResult(
                repository=requested,
                status="intake_failed",
                stop_reason=str(exc),
            )

        return TaskIntakeResult(
            repository=str(context.repository),
            github_repository=context.github_repository,
            candidates_fetched=len(issues) + len(invalid),
            candidates_eligible=len(ranked),
            selected_task=task,
            receipt_path=str(receipt_path),
            status="task_selected",
            stop_reason=(
                "Work was selected and reserved locally; implementation was not started."
            ),
        )

    def _resolve_repository(
        self, repository: Path, configured_repository: Path | None
    ) -> RepositoryContext:
        path = Path(repository).expanduser()
        if path.is_symlink() or not path.is_dir():
            raise IntakeFailure(
                "invalid_repository", "Task intake requires a valid repository directory."
            )
        try:
            requested = path.resolve(strict=True)
            inside = self._git.run(requested, "rev-parse", "--is-inside-work-tree")
            top = self._git.run(requested, "rev-parse", "--show-toplevel")
            if (
                inside.returncode != 0
                or inside.stdout.strip() != "true"
                or top.returncode != 0
            ):
                raise IntakeFailure(
                    "invalid_repository", "Task intake requires a valid Git repository."
                )
            root = Path(top.stdout.strip()).resolve(strict=True)
            if configured_repository is not None and (
                Path(configured_repository).expanduser().resolve(strict=True) != root
            ):
                raise IntakeFailure(
                    "invalid_configuration",
                    "Project configuration belongs to another repository.",
                )
            branch = self._git.run(
                root, "symbolic-ref", "--quiet", "--short", "HEAD"
            )
            head = self._git.run(root, "rev-parse", "--verify", "HEAD")
            origin = self._git.run(root, "remote", "get-url", "origin")
        except IntakeFailure:
            raise
        except (GitCommandError, OSError) as exc:
            raise IntakeFailure(
                "invalid_repository",
                "Repository state could not be inspected safely.",
            ) from exc
        if branch.returncode != 0 or not branch.stdout.strip():
            raise IntakeFailure(
                "invalid_repository", "Task selection requires an attached local branch."
            )
        sha = head.stdout.strip()
        if head.returncode != 0 or COMMIT_SHA.fullmatch(sha) is None:
            raise IntakeFailure(
                "invalid_repository", "Task intake requires a valid HEAD commit."
            )
        if origin.returncode != 0:
            raise IntakeFailure(
                "invalid_repository", "Task intake requires the origin remote."
            )
        github_repository = parse_github_repository(origin.stdout.strip())
        if github_repository is None:
            raise IntakeFailure(
                "invalid_repository",
                "The origin remote must be a supported GitHub HTTPS or SSH URL.",
            )
        return RepositoryContext(
            repository=root,
            github_repository=github_repository,
            branch=branch.stdout.strip(),
            head_sha=sha,
        )

    def _fetch(
        self, context: RepositoryContext, limit: int
    ) -> tuple[list[GitHubIssueCandidate], list[CandidateAssessment]]:
        try:
            preflight = self._github.preflight()
            if preflight.status != "ok":
                message = (
                    "GitHub CLI is required for task intake."
                    if preflight.status == "not_found"
                    else "GitHub CLI must be authenticated for task intake."
                )
                raise IntakeFailure("github_unavailable", message)
            payloads = self._github.list_open_issues(
                context.github_repository, limit=limit
            )
        except IntakeFailure:
            raise
        except GitHubCommandError as exc:
            raise IntakeFailure(
                "github_unavailable", "GitHub Issues could not be read safely."
            ) from exc

        issues = []
        invalid: list[CandidateAssessment] = []
        for payload in payloads[:limit]:
            try:
                issues.append(
                    parse_github_issue(
                        payload, repository=context.github_repository
                    )
                )
            except IssueParseError as exc:
                invalid.append(
                    CandidateAssessment(
                        issue_number=exc.issue_number,
                        decision="invalid",
                        reason="invalid_issue",
                    )
                )
        return issues, invalid


def discover_tasks(repository: Path, **kwargs) -> TaskDiscoveryResult:
    """Convenience boundary used by the CLI and future orchestration."""

    return TaskIntakeService().discover(repository, **kwargs)


def select_task(repository: Path, **kwargs) -> TaskIntakeResult:
    """Convenience boundary used by the CLI and future orchestration."""

    return TaskIntakeService().select(repository, **kwargs)


def _authorization_failure(
    config: IntakeConfig | None, permissions: PermissionsConfig | None
) -> tuple[str, str] | None:
    if config is None:
        return None
    if not config.enabled:
        return "intake_disabled", "Task intake is disabled by project configuration."
    if permissions is None or not permissions.read_issues:
        return (
            "invalid_configuration",
            "Project configuration must explicitly allow read_issues.",
        )
    return None


def _requested_path(repository: Path) -> str:
    return str(Path(repository).expanduser().resolve(strict=False))


def _task_spec(
    context: RepositoryContext,
    selected: CandidateAssessment,
    selected_at: datetime,
) -> TaskSpec:
    issue = selected.issue
    assert issue is not None
    return TaskSpec(
        task_id=f"github:{context.github_repository}#{issue.number}",
        repository=str(context.repository),
        issue_number=issue.number,
        issue_url=issue.url,
        title=issue.title,
        description=issue.body,
        description_truncated=issue.body_truncated,
        labels=issue.labels,
        priority=selected.priority,
        base_branch=context.branch,
        base_commit_sha=context.head_sha,
        selected_at=selected_at,
    )


def _receipt(
    context: RepositoryContext,
    selected: CandidateAssessment,
    selected_at: datetime,
) -> TaskIntakeReceipt:
    issue = selected.issue
    assert issue is not None
    return TaskIntakeReceipt(
        repository=str(context.repository),
        github_repository=context.github_repository,
        issue_number=issue.number,
        issue_url=issue.url,
        title=issue.title,
        selected_priority=selected.priority,
        labels=issue.labels,
        base_branch=context.branch,
        base_commit_sha=context.head_sha,
        selected_at=selected_at,
    )
