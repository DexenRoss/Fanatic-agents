"""Strict, bounded contracts for read-only task intake."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from fanatic_agents.core.project import NonEmptyStrictString, StrictModel

MAX_ISSUE_BODY_CHARS = 20_000
MAX_ISSUE_TITLE_CHARS = 500
Priority = Literal["p0", "p1", "p2", "p3", "none"]
DecisionKind = Literal["eligible", "ineligible", "invalid"]
DecisionReason = Literal[
    "eligible",
    "missing_required_label",
    "blocked_label",
    "closed",
    "ambiguous_priority",
    "duplicate_active_receipt",
    "manual_intervention_required",
    "invalid_issue",
]
TaskIntakeStatus = Literal[
    "task_selected",
    "no_eligible_tasks",
    "github_unavailable",
    "invalid_repository",
    "invalid_configuration",
    "ambiguous_priority",
    "intake_disabled",
    "intake_failed",
]
ISSUE_URL = re.compile(
    r"^https://github\.com/(?P<repository>[^/]+/[^/]+)/issues/(?P<number>[1-9][0-9]*)/?$",
    re.IGNORECASE,
)
GIT_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")
TASK_ID = re.compile(
    r"^github:(?P<repository>[^/]+/[^#]+)#(?P<number>[1-9][0-9]*)$",
    re.IGNORECASE,
)


class GitHubIssueCandidate(StrictModel):
    """A normalized Issue snapshot whose title and body are always untrusted."""

    repository: NonEmptyStrictString
    number: int = Field(strict=True, gt=0)
    title: NonEmptyStrictString = Field(max_length=MAX_ISSUE_TITLE_CHARS)
    body: str = Field(strict=True, max_length=MAX_ISSUE_BODY_CHARS)
    body_truncated: bool = False
    url: NonEmptyStrictString
    state: Literal["open", "closed"]
    labels: list[NonEmptyStrictString] = Field(default_factory=list, max_length=100)
    assignees: list[NonEmptyStrictString] = Field(default_factory=list, max_length=100)
    author: NonEmptyStrictString | None = None
    created_at: datetime
    updated_at: datetime
    milestone: NonEmptyStrictString | None = None
    source: Literal["github_issue"] = "github_issue"
    source_content_trusted: Literal[False] = False

    @model_validator(mode="after")
    def validate_identity(self) -> "GitHubIssueCandidate":
        match = ISSUE_URL.fullmatch(self.url)
        if (
            match is None
            or match.group("repository").casefold() != self.repository.casefold()
            or int(match.group("number")) != self.number
        ):
            raise ValueError("Issue URL does not match its repository and number")
        if self.created_at.utcoffset() is None or self.updated_at.utcoffset() is None:
            raise ValueError("Issue timestamps must include a timezone")
        if self.updated_at < self.created_at:
            raise ValueError("Issue updated_at cannot precede created_at")
        return self


class CandidateAssessment(StrictModel):
    """One deterministic policy decision with a machine-readable reason."""

    issue: GitHubIssueCandidate | None = None
    issue_number: int | None = Field(default=None, strict=True, gt=0)
    decision: DecisionKind
    reason: DecisionReason
    priority: Priority = "none"

    @model_validator(mode="after")
    def validate_issue_number(self) -> "CandidateAssessment":
        if self.issue is not None and self.issue_number != self.issue.number:
            raise ValueError("assessment identity must match its Issue")
        return self


class TaskSpec(StrictModel):
    """Bounded hand-off contract for a future workflow; it grants no permissions."""

    task_id: NonEmptyStrictString
    source: Literal["github_issue"] = "github_issue"
    repository: NonEmptyStrictString
    issue_number: int = Field(strict=True, gt=0)
    issue_url: NonEmptyStrictString
    title: NonEmptyStrictString
    description: str = Field(strict=True, max_length=MAX_ISSUE_BODY_CHARS)
    description_truncated: bool = False
    labels: list[NonEmptyStrictString] = Field(default_factory=list)
    priority: Priority
    base_branch: NonEmptyStrictString
    base_commit_sha: NonEmptyStrictString
    selected_at: datetime
    source_content_trusted: Literal[False] = False

    @model_validator(mode="after")
    def validate_provenance(self) -> "TaskSpec":
        task_match = TASK_ID.fullmatch(self.task_id)
        url_match = ISSUE_URL.fullmatch(self.issue_url)
        if (
            task_match is None
            or url_match is None
            or task_match.group("repository").casefold()
            != url_match.group("repository").casefold()
            or int(task_match.group("number")) != self.issue_number
            or int(url_match.group("number")) != self.issue_number
            or GIT_SHA.fullmatch(self.base_commit_sha) is None
            or self.selected_at.utcoffset() is None
        ):
            raise ValueError("TaskSpec provenance is invalid")
        return self



TaskStatus = Literal[
    "selected",
    "running",
    "verified",
    "promoted",
    "delivered",
    "waiting_for_ci",
    "waiting_for_review",
    "ready_for_human_merge",
    "merged_externally",
    "completed",
    "cancelled",
    "failed",
]


class TaskIntakeReceipt(StrictModel):
    """Secret-free local reservation for exactly one selected GitHub Issue."""

    receipt_version: Literal[1] = 1
    repository: NonEmptyStrictString
    github_repository: NonEmptyStrictString
    issue_number: int = Field(strict=True, gt=0)
    issue_url: NonEmptyStrictString
    title: NonEmptyStrictString
    selected_priority: Priority
    labels: list[NonEmptyStrictString] = Field(default_factory=list)
    base_branch: NonEmptyStrictString
    base_commit_sha: NonEmptyStrictString
    selected_at: datetime
    source: Literal["github_issue"] = "github_issue"
    task_status: TaskStatus = "selected"
    source_content_trusted: Literal[False] = False

    @model_validator(mode="after")
    def validate_provenance(self) -> "TaskIntakeReceipt":
        url_match = ISSUE_URL.fullmatch(self.issue_url)
        if (
            url_match is None
            or url_match.group("repository").casefold()
            != self.github_repository.casefold()
            or int(url_match.group("number")) != self.issue_number
            or GIT_SHA.fullmatch(self.base_commit_sha) is None
            or self.selected_at.utcoffset() is None
        ):
            raise ValueError("Task intake receipt provenance is invalid")
        return self



class TaskDiscoveryResult(StrictModel):
    """Read-only discovery snapshot; it never represents a reservation."""

    repository: NonEmptyStrictString
    github_repository: NonEmptyStrictString | None = None
    candidates_fetched: int = Field(default=0, strict=True, ge=0)
    candidates_eligible: int = Field(default=0, strict=True, ge=0)
    eligible_candidates: list[GitHubIssueCandidate] = Field(default_factory=list)
    assessments: list[CandidateAssessment] = Field(default_factory=list)
    status: Literal[
        "tasks_discovered",
        "no_eligible_tasks",
        "github_unavailable",
        "invalid_repository",
        "invalid_configuration",
        "intake_disabled",
        "intake_failed",
    ]
    stop_reason: str | None = None

    @property
    def final_status(self) -> str:
        return self.status.upper()


class TaskIntakeResult(StrictModel):
    """Terminal result for selecting at most one locally reserved task."""

    repository: NonEmptyStrictString
    github_repository: NonEmptyStrictString | None = None
    candidates_fetched: int = Field(default=0, strict=True, ge=0)
    candidates_eligible: int = Field(default=0, strict=True, ge=0)
    selected_task: TaskSpec | None = None
    receipt_path: NonEmptyStrictString | None = None
    status: TaskIntakeStatus
    stop_reason: str | None = None

    @property
    def final_status(self) -> str:
        return self.status.upper()


class IssueParseError(ValueError):
    """A GitHub Issue payload could not be normalized safely."""

    def __init__(self, message: str, *, issue_number: int | None = None) -> None:
        super().__init__(message)
        self.issue_number = issue_number


def parse_github_issue(
    payload: dict[str, object], *, repository: str
) -> GitHubIssueCandidate:
    """Normalize one gh JSON object and truncate only its untrusted body."""

    number = _positive_integer(payload.get("number"))
    try:
        title = _string(payload.get("title"))
        body_value = payload.get("body")
        body = "" if body_value is None else _string(body_value, allow_empty=True)
        url = _string(payload.get("url"))
        state = _string(payload.get("state")).casefold()
        if state not in {"open", "closed"}:
            raise ValueError("invalid Issue state")
        labels = _nested_names(payload.get("labels"), "name")
        assignees = _nested_names(payload.get("assignees"), "login")
        author = _optional_nested_name(payload.get("author"), "login")
        milestone = _optional_nested_name(payload.get("milestone"), "title")
        created_at = _datetime(payload.get("createdAt"))
        updated_at = _datetime(payload.get("updatedAt"))
        truncated = len(body) > MAX_ISSUE_BODY_CHARS
        return GitHubIssueCandidate(
            repository=repository,
            number=number,
            title=title,
            body=body[:MAX_ISSUE_BODY_CHARS],
            body_truncated=truncated,
            url=url,
            state=state,
            labels=labels,
            assignees=assignees,
            author=author,
            created_at=created_at,
            updated_at=updated_at,
            milestone=milestone,
        )
    except (TypeError, ValueError) as exc:
        raise IssueParseError(
            "GitHub CLI returned an invalid Issue.", issue_number=number
        ) from exc


def _positive_integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise IssueParseError("GitHub CLI returned an invalid Issue number.")
    return value


def _string(value: object, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError("expected string")
    if not allow_empty and not value.strip():
        raise ValueError("expected non-empty string")
    return value


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("expected timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _nested_names(value: object, field: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("expected list")
    names: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("expected object")
        names.append(_string(item.get(field)))
    return names


def _optional_nested_name(value: object, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("expected object")
    return _string(value.get(field))
