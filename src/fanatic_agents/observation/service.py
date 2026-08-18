"""Deterministic service for read-only pull request observation."""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from fanatic_agents.core.config import PermissionsConfig
from fanatic_agents.delivery.models import PromotionReceipt
from fanatic_agents.delivery.receipt import (
    PromotionReceiptStore,
    ReceiptError,
    repository_identifier,
)
from fanatic_agents.git.promotion import branch_policy_allows
from fanatic_agents.github.client import (
    GitHubCli,
    GitHubCommandError,
    GitHubPreflight,
    parse_pull_request_url,
)
from fanatic_agents.observation.models import (
    CIState,
    CheckConclusion,
    CheckExecutionStatus,
    Mergeability,
    ObservationStatus,
    PullRequestCheck,
    PullRequestObservation,
    PullRequestState,
    ReviewState,
)
from fanatic_agents.observation.receipt import (
    ObservationReceiptError,
    PullRequestObservationStore,
)

MIN_WATCH_INTERVAL_SECONDS = 10.0
MAX_WATCH_TIMEOUT_SECONDS = 1800.0
DEFAULT_WATCH_INTERVAL_SECONDS = 30.0
DEFAULT_WATCH_TIMEOUT_SECONDS = 600.0
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
TERMINAL_WATCH_STATUSES: frozenset[ObservationStatus] = frozenset(
    {
        "ci_failed",
        "changes_requested",
        "ready_for_human_merge",
        "merge_conflict",
        "pr_draft",
        "pr_closed",
        "merged_externally",
        "pr_head_drifted",
        "invalid_delivery",
        "github_unavailable",
        "observation_failed",
    }
)


class GitHubObservationClient(Protocol):
    def preflight(self) -> GitHubPreflight: ...

    def view_pull_request(self, repository: str, number: int) -> dict[str, object]: ...


class ObservationClock(Protocol):
    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemObservationClock:
    """Small injectable clock boundary for bounded polling tests."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ObservationValidationError(RuntimeError):
    """Delivered provenance or the GitHub schema failed closed."""

    def __init__(self, status: ObservationStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class PullRequestObservationService:
    """Observe one delivered PR without invoking Git or mutating GitHub."""

    def __init__(
        self,
        *,
        github: GitHubObservationClient | None = None,
        receipts: PromotionReceiptStore | None = None,
        observations: PullRequestObservationStore | None = None,
        clock: ObservationClock | None = None,
    ) -> None:
        self._github = github or GitHubCli()
        self._receipts = receipts or PromotionReceiptStore()
        self._observations = observations or PullRequestObservationStore()
        self._clock = clock or SystemObservationClock()

    def observe_once(
        self,
        worktree: Path,
        *,
        permissions: PermissionsConfig | None = None,
        configured_repository: Path | None = None,
    ) -> PullRequestObservation:
        requested = Path(worktree).expanduser().resolve(strict=False)
        try:
            receipt = self._receipts.load(requested)
            repository = self._validate_receipt(
                receipt,
                requested,
                permissions=permissions,
                configured_repository=configured_repository,
            )
        except ReceiptError as exc:
            return _failure(requested, "invalid_delivery", str(exc))
        except (ObservationValidationError, OSError) as exc:
            status = (
                exc.status
                if isinstance(exc, ObservationValidationError)
                else "invalid_delivery"
            )
            message = str(exc) if isinstance(exc, ObservationValidationError) else (
                "Delivery provenance could not be validated safely."
            )
            return _failure(requested, status, message)

        fallback = _from_receipt(receipt, repository, "observation_failed")
        try:
            preflight = self._github.preflight()
        except GitHubCommandError:
            return fallback.model_copy(
                update={
                    "status": "github_unavailable",
                    "stop_reason": "GitHub CLI authentication could not be verified.",
                }
            )
        if preflight.status != "ok":
            reason = (
                "GitHub CLI is required for pull request observation."
                if preflight.status == "not_found"
                else "GitHub CLI is installed but not authenticated; run gh auth login."
            )
            return fallback.model_copy(
                update={"status": "github_unavailable", "stop_reason": reason}
            )

        assert receipt.pr_number is not None
        try:
            payload = self._github.view_pull_request(repository, receipt.pr_number)
            observation = _normalize_observation(receipt, repository, payload)
            self._observations.save(observation)
            return observation
        except ObservationValidationError as exc:
            return fallback.model_copy(
                update={"status": exc.status, "stop_reason": str(exc)}
            )
        except (GitHubCommandError, ObservationReceiptError):
            return fallback.model_copy(
                update={
                    "status": "observation_failed",
                    "stop_reason": "The pull request could not be observed safely.",
                }
            )

    def observe_until_terminal(
        self,
        worktree: Path,
        *,
        interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
        timeout_seconds: float = DEFAULT_WATCH_TIMEOUT_SECONDS,
        permissions: PermissionsConfig | None = None,
        configured_repository: Path | None = None,
    ) -> PullRequestObservation:
        if interval_seconds < MIN_WATCH_INTERVAL_SECONDS:
            raise ValueError(
                f"interval_seconds must be at least {MIN_WATCH_INTERVAL_SECONDS:g}"
            )
        if timeout_seconds <= 0 or timeout_seconds > MAX_WATCH_TIMEOUT_SECONDS:
            raise ValueError(
                f"timeout_seconds must be between 1 and {MAX_WATCH_TIMEOUT_SECONDS:g}"
            )

        started = self._clock.monotonic()
        while True:
            observation = self.observe_once(
                worktree,
                permissions=permissions,
                configured_repository=configured_repository,
            )
            if observation.status in TERMINAL_WATCH_STATUSES:
                return observation
            elapsed = self._clock.monotonic() - started
            if elapsed >= timeout_seconds:
                return observation.model_copy(
                    update={
                        "stop_reason": (
                            f"Watch timeout reached after {timeout_seconds:g} seconds; "
                            "no remote state was modified."
                        )
                    }
                )
            self._clock.sleep(min(interval_seconds, timeout_seconds - elapsed))

    @staticmethod
    def _validate_receipt(
        receipt: PromotionReceipt,
        worktree: Path,
        *,
        permissions: PermissionsConfig | None,
        configured_repository: Path | None,
    ) -> str:
        if receipt.delivery_stage != "pr_created":
            raise ObservationValidationError(
                "invalid_delivery",
                "Observation requires a completed Sprint 6 pull request delivery.",
            )
        if (
            receipt.pr_number is None
            or receipt.pr_url is None
            or receipt.commit_sha is None
            or receipt.remote != "origin"
            or receipt.remote_branch != receipt.promoted_branch
            or not COMMIT_SHA.fullmatch(receipt.commit_sha)
        ):
            raise ObservationValidationError(
                "invalid_delivery", "The delivery receipt lacks valid PR provenance."
            )
        if not worktree.is_dir() or worktree.is_symlink():
            raise ObservationValidationError(
                "invalid_delivery",
                "Observation requires an existing real promotion worktree.",
            )
        if Path(receipt.worktree_path).expanduser().resolve(strict=False) != worktree:
            raise ObservationValidationError(
                "invalid_delivery", "The delivery receipt belongs to another worktree."
            )
        repository_path = Path(receipt.repository_path).expanduser()
        if not repository_path.is_dir() or repository_path.is_symlink():
            raise ObservationValidationError(
                "invalid_delivery", "The recorded source repository is unavailable."
            )
        repository_path = repository_path.resolve(strict=True)
        if repository_identifier(repository_path) != receipt.repository_id:
            raise ObservationValidationError(
                "invalid_delivery", "The source repository identity has changed."
            )
        if not branch_policy_allows(receipt.promoted_branch):
            raise ObservationValidationError(
                "invalid_delivery", "Observation permits only validated fanatic/* branches."
            )
        if configured_repository is not None:
            configured = configured_repository.expanduser().resolve(strict=True)
            if configured != repository_path:
                raise ObservationValidationError(
                    "invalid_delivery",
                    "Project configuration belongs to another repository.",
                )
            if permissions is None or not permissions.observe_pull_request:
                raise ObservationValidationError(
                    "invalid_delivery",
                    "Project configuration denies pull request observation.",
                )
        locator = parse_pull_request_url(receipt.pr_url)
        if locator is None or locator[1] != receipt.pr_number:
            raise ObservationValidationError(
                "invalid_delivery", "The delivery receipt has an invalid pull request URL."
            )
        return locator[0]


def observe_once(
    worktree: Path,
    *,
    permissions: PermissionsConfig | None = None,
    configured_repository: Path | None = None,
) -> PullRequestObservation:
    """Public scheduler-friendly entry point for one observation."""
    return PullRequestObservationService().observe_once(
        worktree,
        permissions=permissions,
        configured_repository=configured_repository,
    )


def observe_until_terminal(
    worktree: Path,
    *,
    interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
    timeout_seconds: float = DEFAULT_WATCH_TIMEOUT_SECONDS,
    permissions: PermissionsConfig | None = None,
    configured_repository: Path | None = None,
) -> PullRequestObservation:
    """Public bounded polling entry point with no scheduler or background threads."""
    return PullRequestObservationService().observe_until_terminal(
        worktree,
        interval_seconds=interval_seconds,
        timeout_seconds=timeout_seconds,
        permissions=permissions,
        configured_repository=configured_repository,
    )


def aggregate_ci(checks: list[PullRequestCheck]) -> CIState:
    """Conservatively aggregate every provider reported by GitHub."""
    if not checks:
        return "no_ci_reported"
    failing = {"failure", "cancelled", "timed_out", "action_required"}
    if any(check.conclusion in failing for check in checks):
        return "failed"
    successful = {"success", "skipped", "neutral"}
    if all(
        check.status == "completed" and check.conclusion in successful
        for check in checks
    ):
        return "passed"
    return "pending"


def _normalize_observation(
    receipt: PromotionReceipt,
    repository: str,
    payload: dict[str, object],
) -> PullRequestObservation:
    number = _required_int(payload, "number")
    url = _required_string(payload, "url")
    base = _required_string(payload, "baseRefName")
    head = _required_string(payload, "headRefName")
    head_sha = _required_string(payload, "headRefOid").lower()
    state = _normalize_pr_state(payload.get("state"), payload.get("mergedAt"))
    is_draft = payload.get("isDraft")
    if not isinstance(is_draft, bool):
        raise _schema_error()
    mergeable = _normalize_mergeability(payload.get("mergeable"))
    review_state = _normalize_review(payload.get("reviewDecision"))
    checks = _normalize_checks(payload.get("statusCheckRollup"))
    approvals, changes_requested = _review_counts(payload.get("reviews"))
    if review_state == "approved":
        approvals = max(1, approvals)
    elif review_state == "changes_requested":
        changes_requested = max(1, changes_requested)

    assert receipt.pr_number is not None and receipt.pr_url is not None
    assert receipt.commit_sha is not None
    actual_locator = parse_pull_request_url(url)
    expected_locator = parse_pull_request_url(receipt.pr_url)
    if (
        number != receipt.pr_number
        or actual_locator is None
        or expected_locator is None
        or actual_locator[0].casefold() != repository.casefold()
        or actual_locator != expected_locator
        or base != receipt.base_branch
        or head != receipt.promoted_branch
    ):
        raise ObservationValidationError(
            "invalid_delivery",
            "The observed pull request does not match the delivery receipt.",
        )

    ci_state = aggregate_ci(checks)
    status, reason = _final_state(
        expected_sha=receipt.commit_sha.lower(),
        observed_sha=head_sha,
        pr_state=state,
        is_draft=is_draft,
        mergeable=mergeable,
        ci_state=ci_state,
        review_state=review_state,
    )
    return PullRequestObservation(
        repository=repository,
        promotion_worktree=receipt.worktree_path,
        pr_number=number,
        pr_url=url.rstrip("/"),
        base_branch=base,
        head_branch=head,
        expected_head_sha=receipt.commit_sha.lower(),
        observed_head_sha=head_sha,
        pr_state=state,
        is_draft=is_draft,
        mergeable=mergeable,
        review_state=review_state,
        approvals=approvals,
        changes_requested=changes_requested,
        checks=checks,
        ci_state=ci_state,
        status=status,
        stop_reason=reason,
        observed_at=datetime.now(UTC),
    )


def _normalize_checks(raw: object) -> list[PullRequestCheck]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise _schema_error()
    return [_normalize_check(item) for item in raw]


def _normalize_check(raw: object) -> PullRequestCheck:
    if not isinstance(raw, dict):
        raise _schema_error()
    if raw.get("__typename") == "StatusContext" or "context" in raw:
        name = _required_string(raw, "context")
        raw_state = _optional_upper_string(raw.get("state"))
        if raw_state in {"EXPECTED", "PENDING"}:
            status: CheckExecutionStatus = "pending"
            conclusion: CheckConclusion | None = None
        else:
            status = "completed" if raw_state in {"SUCCESS", "ERROR", "FAILURE"} else "unknown"
            conclusion = {
                "SUCCESS": "success",
                "ERROR": "failure",
                "FAILURE": "failure",
            }.get(raw_state, "unknown")
        return PullRequestCheck(
            name=name,
            context=name,
            status=status,
            conclusion=conclusion,
            details_url=_optional_string(raw.get("targetUrl")),
        )

    name = _required_string(raw, "name")
    raw_status = _optional_upper_string(raw.get("status"))
    status_map: dict[str, CheckExecutionStatus] = {
        "QUEUED": "queued",
        "PENDING": "pending",
        "IN_PROGRESS": "in_progress",
        "WAITING": "waiting",
        "REQUESTED": "requested",
        "COMPLETED": "completed",
    }
    status = status_map.get(raw_status, "unknown")
    raw_conclusion = _optional_upper_string(raw.get("conclusion"))
    conclusion_map: dict[str, CheckConclusion] = {
        "SUCCESS": "success",
        "FAILURE": "failure",
        "CANCELLED": "cancelled",
        "SKIPPED": "skipped",
        "NEUTRAL": "neutral",
        "TIMED_OUT": "timed_out",
        "ACTION_REQUIRED": "action_required",
        "STARTUP_FAILURE": "failure",
        "STALE": "failure",
    }
    conclusion = None if raw_conclusion is None and status != "completed" else (
        conclusion_map.get(raw_conclusion, "unknown")
    )
    return PullRequestCheck(
        name=name,
        context=_optional_string(raw.get("workflowName")),
        status=status,
        conclusion=conclusion,
        details_url=_optional_string(raw.get("detailsUrl")),
    )


def _review_counts(raw: object) -> tuple[int, int]:
    if raw is None:
        return 0, 0
    if not isinstance(raw, list):
        raise _schema_error()
    latest: dict[str, str] = {}
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _schema_error()
        state = _optional_upper_string(item.get("state"))
        author = item.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        key = login if isinstance(login, str) and login else f"review-{index}"
        if state is not None:
            latest[key] = state
    return (
        sum(state == "APPROVED" for state in latest.values()),
        sum(state == "CHANGES_REQUESTED" for state in latest.values()),
    )


def _normalize_pr_state(raw: object, merged_at: object) -> PullRequestState:
    if merged_at is not None:
        if not isinstance(merged_at, str):
            raise _schema_error()
        return "merged"
    value = _optional_upper_string(raw)
    return {"OPEN": "open", "CLOSED": "closed", "MERGED": "merged"}.get(
        value, "unknown"
    )


def _normalize_mergeability(raw: object) -> Mergeability:
    value = _optional_upper_string(raw)
    return {"MERGEABLE": "mergeable", "CONFLICTING": "conflicting"}.get(
        value, "unknown"
    )


def _normalize_review(raw: object) -> ReviewState:
    if raw is None or raw == "":
        return "none"
    value = _optional_upper_string(raw)
    return {
        "APPROVED": "approved",
        "CHANGES_REQUESTED": "changes_requested",
        "REVIEW_REQUIRED": "review_required",
    }.get(value, "unknown")


def _final_state(
    *,
    expected_sha: str,
    observed_sha: str,
    pr_state: PullRequestState,
    is_draft: bool,
    mergeable: Mergeability,
    ci_state: CIState,
    review_state: ReviewState,
) -> tuple[ObservationStatus, str | None]:
    if observed_sha != expected_sha:
        return "pr_head_drifted", "The PR head no longer matches the delivered commit."
    if pr_state == "merged":
        return "merged_externally", "The pull request was merged by a human or another tool."
    if pr_state == "closed":
        return "pr_closed", "The pull request was closed without a recorded merge."
    if pr_state != "open":
        return "observation_failed", "GitHub returned an unknown pull request state."
    if is_draft:
        return "pr_draft", "The pull request is still a draft."
    if mergeable == "conflicting":
        return "merge_conflict", "GitHub reports merge conflicts."
    if ci_state == "failed":
        return "ci_failed", "At least one observed check failed or was cancelled."
    if review_state == "changes_requested":
        return "changes_requested", "Human review requested changes."
    if ci_state == "pending":
        return "waiting_for_ci", "Observed checks are pending or unknown."
    if ci_state == "no_ci_reported":
        return "no_ci_reported", "GitHub has not reported any checks."
    if review_state == "approved":
        return "ready_for_human_merge", None
    return "waiting_for_review", "CI passed; human approval is still required."


def _from_receipt(
    receipt: PromotionReceipt,
    repository: str,
    status: ObservationStatus,
) -> PullRequestObservation:
    return PullRequestObservation(
        repository=repository,
        promotion_worktree=receipt.worktree_path,
        pr_number=receipt.pr_number,
        pr_url=receipt.pr_url,
        base_branch=receipt.base_branch,
        head_branch=receipt.promoted_branch,
        expected_head_sha=receipt.commit_sha,
        status=status,
        observed_at=datetime.now(UTC),
    )


def _failure(
    worktree: Path, status: ObservationStatus, reason: str
) -> PullRequestObservation:
    return PullRequestObservation(
        repository=str(worktree),
        promotion_worktree=str(worktree),
        status=status,
        stop_reason=reason,
        observed_at=datetime.now(UTC),
    )


def _required_string(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _schema_error()
    return value.strip()


def _required_int(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _schema_error()
    return value


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise _schema_error()
    stripped = value.strip()
    return stripped or None


def _optional_upper_string(value: object) -> str | None:
    normalized = _optional_string(value)
    return normalized.upper() if normalized is not None else None


def _schema_error() -> ObservationValidationError:
    return ObservationValidationError(
        "observation_failed", "GitHub returned an unexpected pull request schema."
    )
