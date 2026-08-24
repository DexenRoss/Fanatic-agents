"""Service-layer composition for one manually triggered autonomous task."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fanatic_agents.autonomous.branch import (
    BranchAvailabilityService,
    autonomous_branch_name,
)
from fanatic_agents.autonomous.models import (
    AutonomousRunReceipt,
    AutonomousRunResult,
    AutonomousTransition,
)
from fanatic_agents.autonomous.receipt import (
    AutonomousReceiptError,
    AutonomousRunLockedError,
    AutonomousRunReceiptStore,
)
from fanatic_agents.core.config import ProjectConfig
from fanatic_agents.delivery.models import DeliveryResult
from fanatic_agents.delivery.service import DeliveryService
from fanatic_agents.git.errors import RepositoryStateError
from fanatic_agents.git.inspection import (
    RepositoryInspectionError,
    RepositoryInspector,
    RepositorySnapshot,
)
from fanatic_agents.git.models import BaseRepositoryState, PromotionResult
from fanatic_agents.git.promotion import (
    VerifiedChangePromotionService,
    capture_base_repository_state,
)
from fanatic_agents.github.client import GitHubCli, GitHubCommandError
from fanatic_agents.implementation.models import ImplementationResult
from fanatic_agents.implementation.service import ControlledImplementationService
from fanatic_agents.intake.models import (
    IssueParseError,
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
from fanatic_agents.intake.service import TaskIntakeService
from fanatic_agents.observation.models import PullRequestObservation
from fanatic_agents.observation.service import PullRequestObservationService
from fanatic_agents.orchestrator.models import WorkflowResult
from fanatic_agents.orchestrator.workflow import WorkflowOrchestrator


class IntakeRunner(Protocol):
    def select(self, repository: Path, **kwargs: object) -> TaskIntakeResult: ...


class FreshIssueClient(Protocol):
    def view_issue(self, repository: str, number: int) -> dict[str, object]: ...


class SnapshotInspector(Protocol):
    def inspect(self, repository: Path) -> RepositorySnapshot: ...


class WorkflowRunner(Protocol):
    def run(
        self, snapshot: RepositorySnapshot, task_spec: TaskSpec | None = None
    ) -> WorkflowResult: ...


class ImplementationRunner(Protocol):
    def run(self, **kwargs: object) -> ImplementationResult: ...


class PromotionRunner(Protocol):
    def promote(self, **kwargs: object) -> PromotionResult: ...


class DeliveryRunner(Protocol):
    def deliver(self, worktree: Path, **kwargs: object) -> DeliveryResult: ...


class ObservationRunner(Protocol):
    def observe_once(
        self, worktree: Path, **kwargs: object
    ) -> PullRequestObservation: ...


class BranchChecker(Protocol):
    def check(self, repository: Path, branch: str) -> str: ...


class AutonomousRunner:
    """Compose existing services once, with deterministic gates between phases."""

    def __init__(
        self,
        *,
        intake: IntakeRunner | None = None,
        github: FreshIssueClient | None = None,
        task_receipts: TaskIntakeReceiptStore | None = None,
        run_receipts: AutonomousRunReceiptStore | None = None,
        inspector: SnapshotInspector | None = None,
        workflow: WorkflowRunner | None = None,
        implementation: ImplementationRunner | None = None,
        promotion: PromotionRunner | None = None,
        delivery: DeliveryRunner | None = None,
        observation: ObservationRunner | None = None,
        branches: BranchChecker | None = None,
        capture_base: Callable[[Path], BaseRepositoryState] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._task_receipts = task_receipts or TaskIntakeReceiptStore()
        self._intake = intake or TaskIntakeService(receipts=self._task_receipts)
        self._github = github or GitHubCli()
        self._run_receipts = run_receipts or AutonomousRunReceiptStore()
        self._inspector = inspector or RepositoryInspector()
        self._workflow = workflow or WorkflowOrchestrator()
        self._implementation = implementation or ControlledImplementationService()
        self._promotion = promotion or VerifiedChangePromotionService()
        self._delivery = delivery or DeliveryService()
        self._observation = observation or PullRequestObservationService()
        self._branches = branches or BranchAvailabilityService()
        self._capture_base = capture_base or capture_base_repository_state
        self._clock = clock or (lambda: datetime.now(UTC))

    def run_once(
        self,
        project_config: ProjectConfig,
        *,
        image: str,
        repository: Path | None = None,
        deliver: bool = False,
    ) -> AutonomousRunResult:
        """Process at most one selected Issue and stop without merge or Issue mutation."""
        started = self._clock()
        requested = (
            Path(repository)
            if repository is not None
            else Path(project_config.repository.path)
        ).expanduser()
        repository_text = str(requested.resolve(strict=False))
        denied = _initial_authorization(project_config)
        if denied is not None:
            return self._finish(
                started, repository_text, denied[0], denied[1]
            )

        try:
            with self._run_receipts.lock(requested) as run_lock:
                return self._run_locked(
                    project_config,
                    requested,
                    repository_text,
                    image=image,
                    delivery_authorized=deliver,
                    started=started,
                    run_lock=run_lock,
                )
        except AutonomousRunLockedError as exc:
            return self._finish(
                started,
                repository_text,
                "autonomous_run_failed",
                str(exc),
            )
        except (
            AutonomousReceiptError,
            TaskIntakeReceiptError,
            OSError,
            ValueError,
        ):
            return self._finish(
                started,
                repository_text,
                "autonomous_run_failed",
                "Autonomous metadata failed closed; no ambiguous run was continued.",
            )

    def _run_locked(
        self,
        config: ProjectConfig,
        repository: Path,
        repository_text: str,
        *,
        image: str,
        delivery_authorized: bool,
        started: datetime,
        run_lock: object,
    ) -> AutonomousRunResult:
        intake = self._intake.select(
            repository,
            intake_config=config.intake,
            permissions=config.permissions,
            configured_repository=Path(config.repository.path),
        )
        if intake.status != "task_selected" or intake.selected_task is None:
            status = (
                "no_eligible_tasks"
                if intake.status in {"no_eligible_tasks", "ambiguous_priority"}
                else "github_unavailable"
                if intake.status == "github_unavailable"
                else "autonomous_run_failed"
            )
            return self._finish(
                started,
                intake.repository or repository_text,
                status,
                intake.stop_reason,
                github_repository=intake.github_repository,
            )

        task = intake.selected_task
        if hasattr(run_lock, "set_task_id"):
            run_lock.set_task_id(task.task_id)
        common = _task_fields(task)
        run_receipt = AutonomousRunReceipt(
            intake_receipt_path=intake.receipt_path or "unavailable",
            repository=task.repository,
            github_repository=intake.github_repository or _task_github_repository(task),
            task_id=task.task_id,
            issue_number=task.issue_number,
            issue_url=task.issue_url,
            task_title=task.title,
            base_branch=task.base_branch,
            base_commit_sha=task.base_commit_sha,
            task_status="selected",
            transitions=[AutonomousTransition(state="selected", at=started)],
            started_at=started,
            updated_at=started,
        )
        try:
            self._run_receipts.save(run_receipt)
            task_receipt = self._task_receipts.claim(repository, task.issue_number)
            run_receipt = self._run_receipts.transition(run_receipt, "running")
        except (TaskIntakeReceiptError, AutonomousReceiptError):
            return self._finish(
                started,
                task.repository,
                "autonomous_run_failed",
                "The selected task could not be claimed atomically.",
                **common,
            )

        freshness = self._fresh_task(task, config)
        if freshness != "eligible":
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "github_unavailable" if freshness == "unavailable" else "task_revoked",
                "The selected Issue could not be refreshed safely."
                if freshness == "unavailable"
                else "The selected Issue is no longer eligible; no agent was called.",
                task_status="failed",
                **common,
            )

        try:
            base = self._capture_base(repository)
        except RepositoryStateError as exc:
            self._mark_failed(task_receipt, run_receipt)
            status = (
                "repository_dirty"
                if exc.status == "repository_dirty"
                else "base_repository_drifted"
            )
            return self._finish(
                started,
                task.repository,
                status,
                str(exc),
                task_status="failed",
                **common,
            )
        except Exception:
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "base_repository_drifted",
                "Repository state could not be validated safely.",
                task_status="failed",
                **common,
            )
        if not base.working_tree_clean:
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "repository_dirty",
                "Autonomous execution requires a clean working tree; it was not repaired.",
                task_status="failed",
                **common,
            )
        if (
            base.branch != task.base_branch
            or base.commit_sha.casefold() != task.base_commit_sha.casefold()
            or Path(base.repository_path).resolve(strict=True)
            != Path(task.repository).resolve(strict=True)
        ):
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "base_repository_drifted",
                "Repository branch or HEAD changed after task selection.",
                task_status="failed",
                **common,
            )

        try:
            snapshot = self._inspector.inspect(repository)
        except (RepositoryInspectionError, OSError, ValueError):
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "autonomous_run_failed",
                "Repository inspection failed before agents started.",
                task_status="failed",
                **common,
            )

        try:
            workflow = self._workflow.run(snapshot, task)
        except Exception:
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "workflow_rejected",
                "The task-aware workflow failed safely.",
                task_status="failed",
                model_calls=1,
                **common,
            )
        model_calls = workflow.model_calls
        run_receipt = self._run_receipts.update(
            run_receipt, model_calls=model_calls
        )
        if workflow.status != "ready_for_implementation":
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "workflow_rejected",
                workflow.stop_reason,
                task_status="failed",
                workflow_status=workflow.status,
                model_calls=model_calls,
                **common,
            )

        implementation = self._implementation.run(
            repository=repository,
            snapshot=snapshot,
            workflow=workflow,
            image=image,
            base_repository_state=base,
        )
        implementation_call = 0 if implementation.status == "policy_rejected" and implementation.changeset is None else 1
        model_calls = min(5, model_calls + implementation_call)
        run_receipt = self._run_receipts.update(
            run_receipt, model_calls=model_calls
        )
        if implementation.status != "verified":
            self._mark_failed(task_receipt, run_receipt)
            status = (
                "verification_failed"
                if implementation.status == "verification_failed"
                else "implementation_failed"
            )
            return self._finish(
                started,
                task.repository,
                status,
                implementation.stop_reason,
                task_status="failed",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                model_calls=model_calls,
                **common,
            )

        task_receipt = self._task_receipts.transition(task_receipt, "verified")
        run_receipt = self._run_receipts.transition(
            run_receipt, "verified", model_calls=model_calls
        )
        if not config.autonomy.auto_promote:
            return self._finish(
                started,
                task.repository,
                "verified",
                None,
                task_status="verified",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                model_calls=model_calls,
                **common,
            )

        branch = autonomous_branch_name(task.issue_number, task.title)
        if not config.permissions.create_branch:
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "permission_denied",
                "Project configuration denies autonomous branch creation.",
                branch=branch,
                task_status="failed",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                model_calls=model_calls,
                **common,
            )
        availability = self._branches.check(repository, branch)
        if availability == "exists":
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "branch_already_exists",
                "The deterministic local or remote branch already exists.",
                branch=branch,
                task_status="failed",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                model_calls=model_calls,
                **common,
            )
        if availability != "available":
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "promotion_failed",
                "Branch availability could not be proven safely.",
                branch=branch,
                task_status="failed",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                model_calls=model_calls,
                **common,
            )

        promotion = self._promotion.promote(
            repository=repository,
            implementation=implementation,
            branch=branch,
            files_likely_affected=workflow.developer.files_likely_affected
            if workflow.developer is not None
            else [],
        )
        if promotion.status != "promoted" or promotion.worktree_path is None:
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "promotion_failed",
                promotion.stop_reason,
                branch=branch,
                task_status="failed",
                workflow_status=workflow.status,
                implementation_status=implementation.status,
                promotion_status=promotion.status,
                model_calls=model_calls,
                **common,
            )

        task_receipt = self._task_receipts.transition(task_receipt, "promoted")
        run_receipt = self._run_receipts.transition(
            run_receipt,
            "promoted",
            branch=branch,
            worktree_path=promotion.worktree_path,
            promotion_status=promotion.status,
        )
        promoted_fields = {
            **common,
            "branch": branch,
            "task_status": "promoted",
            "workflow_status": workflow.status,
            "implementation_status": implementation.status,
            "promotion_status": promotion.status,
            "worktree_path": promotion.worktree_path,
            "model_calls": model_calls,
        }
        if not config.autonomy.auto_deliver:
            return self._finish(
                started, task.repository, "promoted", None, **promoted_fields
            )
        if not delivery_authorized:
            return self._finish(
                started,
                task.repository,
                "promoted",
                "Autonomous delivery was not authorized by the CLI --deliver gate.",
                delivery_status="not_performed",
                **promoted_fields,
            )
        missing = [
            name
            for name in ("commit", "push_branch", "create_pull_request")
            if not getattr(config.permissions, name)
        ]
        if missing:
            return self._finish(
                started,
                task.repository,
                "permission_denied",
                "Autonomous delivery permissions are denied: " + ", ".join(missing),
                delivery_status="permission_denied",
                **promoted_fields,
            )

        commit_message = _bounded_subject(
            f"fanatic: issue #{task.issue_number} {task.title}"
        )
        pr_title = _bounded_subject(
            f"fanatic: #{task.issue_number} {task.title}"
        )
        pr_body = (
            "## Fanatic Agents Autonomous Delivery\n\n"
            f"Task:\n{task.title}\n\n"
            f"Source Issue:\n#{task.issue_number} {task.issue_url}\n\n"
            "Implementation: VERIFIED\n\nPromotion: PROMOTED\n\n"
            "Safety:\n- no automatic merge was performed\n"
            "- the source Issue was not modified\n\n"
            "Generated by Fanatic Agents."
        )
        delivery = self._delivery.deliver(
            Path(promotion.worktree_path),
            permissions=config.permissions,
            configured_repository=Path(config.repository.path),
            commit_message=commit_message,
            pr_title=pr_title,
            pr_body=pr_body,
        )
        if delivery.status != "delivered":
            self._mark_failed(task_receipt, run_receipt)
            return self._finish(
                started,
                task.repository,
                "delivery_failed",
                delivery.stop_reason,
                delivery_status=delivery.status,
                commit_sha=delivery.commit_sha,
                pr_number=delivery.pr_number,
                pr_url=delivery.pr_url,
                **{**promoted_fields, "task_status": "failed"},
            )

        task_receipt = self._task_receipts.transition(task_receipt, "delivered")
        run_receipt = self._run_receipts.transition(
            run_receipt,
            "delivered",
            delivery_status=delivery.status,
            commit_sha=delivery.commit_sha,
            pr_number=delivery.pr_number,
            pr_url=delivery.pr_url,
        )
        delivered_fields = {
            **promoted_fields,
            "task_status": "delivered",
            "delivery_status": delivery.status,
            "commit_sha": delivery.commit_sha,
            "pr_number": delivery.pr_number,
            "pr_url": delivery.pr_url,
        }
        if (
            not config.autonomy.observe_after_delivery
            or not config.permissions.observe_pull_request
        ):
            return self._finish(
                started,
                task.repository,
                "delivered_for_review",
                None,
                **delivered_fields,
            )

        observation = self._observation.observe_once(
            Path(promotion.worktree_path),
            permissions=config.permissions,
            configured_repository=Path(config.repository.path),
        )
        observation_state = _observation_task_state(observation.status)
        final_status = _observation_final_status(observation.status)
        if observation_state is not None:
            task_receipt = self._task_receipts.transition(
                task_receipt, observation_state
            )
            run_receipt = self._run_receipts.transition(
                run_receipt,
                observation_state,
                observation_status=observation.status,
            )
            delivered_fields["task_status"] = observation_state
        else:
            run_receipt = self._run_receipts.update(
                run_receipt, observation_status=observation.status
            )
        return self._finish(
            started,
            task.repository,
            final_status,
            observation.stop_reason,
            observation_status=observation.status,
            **delivered_fields,
        )

    def _fresh_task(
        self, task: TaskSpec, config: ProjectConfig
    ) -> str:
        github_repository = task.task_id.split(":", 1)[1].split("#")[0]
        try:
            payload = self._github.view_issue(
                github_repository, task.issue_number
            )
            issue = parse_github_issue(
                payload,
                repository=github_repository,
            )
        except GitHubCommandError:
            return "unavailable"
        except (IssueParseError, KeyError, ValueError):
            return "revoked"
        assessment = TaskIntakePolicy(config.intake).evaluate(issue)
        if (
            assessment.decision != "eligible"
            or issue.number != task.issue_number
            or issue.repository.casefold() != github_repository.casefold()
        ):
            return "revoked"
        return "eligible"

    def _mark_failed(
        self,
        task_receipt: TaskIntakeReceipt,
        run_receipt: AutonomousRunReceipt,
    ) -> None:
        if task_receipt.task_status not in {"failed", "cancelled", "completed"}:
            self._task_receipts.transition(task_receipt, "failed")
        if run_receipt.task_status != "failed":
            self._run_receipts.transition(run_receipt, "failed")

    def _finish(
        self,
        started: datetime,
        repository: str,
        status: str,
        reason: str | None,
        **fields: object,
    ) -> AutonomousRunResult:
        return AutonomousRunResult(
            repository=repository,
            started_at=started,
            finished_at=self._clock(),
            status=status,
            stop_reason=reason,
            **fields,
        )


def run_once(
    project_config: ProjectConfig,
    *,
    image: str,
    repository: Path | None = None,
    deliver: bool = False,
) -> AutonomousRunResult:
    """Scheduler-independent public boundary for one autonomous pass."""
    return AutonomousRunner().run_once(
        project_config,
        image=image,
        repository=repository,
        deliver=deliver,
    )


def _initial_authorization(config: ProjectConfig) -> tuple[str, str] | None:
    if not config.autonomy.enabled:
        return "autonomy_disabled", "Project autonomy is disabled."
    if not config.intake.enabled:
        return "permission_denied", "Task intake must be explicitly enabled."
    if not config.permissions.read_issues:
        return "permission_denied", "Project configuration denies read_issues."
    if not config.permissions.autonomous_execution:
        return "permission_denied", "Project configuration denies autonomous_execution."
    return None


def _task_github_repository(task: TaskSpec) -> str:
    return task.task_id.split(":", 1)[1].split("#")[0]


def _task_fields(task: TaskSpec) -> dict[str, object]:
    github_repository = _task_github_repository(task)
    return {
        "github_repository": github_repository,
        "issue_number": task.issue_number,
        "issue_url": task.issue_url,
        "task_id": task.task_id,
        "task_title": task.title,
        "priority": task.priority,
    }


def _observation_task_state(status: str) -> str | None:
    return {
        "waiting_for_ci": "waiting_for_ci",
        "no_ci_reported": "waiting_for_ci",
        "waiting_for_review": "waiting_for_review",
        "ready_for_human_merge": "ready_for_human_merge",
        "merged_externally": "merged_externally",
    }.get(status)


def _observation_final_status(status: str) -> str:
    return {
        "waiting_for_ci": "waiting_for_ci",
        "no_ci_reported": "waiting_for_ci",
        "waiting_for_review": "waiting_for_review",
        "ready_for_human_merge": "ready_for_human_merge",
    }.get(status, "delivered_for_review")


def _bounded_subject(value: str) -> str:
    return " ".join(value.split())[:200]
