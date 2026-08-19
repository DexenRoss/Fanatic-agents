"""Deterministic opt-in policy and ranking for GitHub Issue intake."""

from __future__ import annotations

from fanatic_agents.core.config import IntakeConfig
from fanatic_agents.intake.models import (
    CandidateAssessment,
    GitHubIssueCandidate,
    Priority,
)

PRIORITY_LABELS: tuple[str, ...] = (
    "priority:p0",
    "priority:p1",
    "priority:p2",
    "priority:p3",
)
PRIORITY_RANK = {label: rank for rank, label in enumerate(PRIORITY_LABELS)}


class TaskIntakePolicy:
    """Classify Issues without interpreting their title or body."""

    def __init__(self, config: IntakeConfig) -> None:
        self._required = {label.casefold() for label in config.required_labels}
        self._blocked = {label.casefold() for label in config.blocked_labels}

    def evaluate(
        self,
        issue: GitHubIssueCandidate,
        *,
        active_issue_numbers: set[int] | None = None,
    ) -> CandidateAssessment:
        labels = {label.casefold() for label in issue.labels}
        priority_labels = labels & set(PRIORITY_LABELS)
        priority = self.priority(issue)

        if issue.state != "open":
            return _assessment(issue, "ineligible", "closed", priority)
        if len(priority_labels) > 1:
            return _assessment(issue, "invalid", "ambiguous_priority", "none")
        if not self._required.issubset(labels):
            return _assessment(
                issue, "ineligible", "missing_required_label", priority
            )
        if labels & self._blocked:
            return _assessment(issue, "ineligible", "blocked_label", priority)
        if issue.number in (active_issue_numbers or set()):
            return _assessment(
                issue, "ineligible", "duplicate_active_receipt", priority
            )
        return _assessment(issue, "eligible", "eligible", priority)

    @staticmethod
    def priority(issue: GitHubIssueCandidate) -> Priority:
        matched = [
            label for label in (item.casefold() for item in issue.labels)
            if label in PRIORITY_RANK
        ]
        if len(set(matched)) != 1:
            return "none"
        return matched[0].removeprefix("priority:")  # type: ignore[return-value]

    @staticmethod
    def rank(
        assessments: list[CandidateAssessment],
    ) -> list[CandidateAssessment]:
        eligible = [
            assessment
            for assessment in assessments
            if assessment.decision == "eligible" and assessment.issue is not None
        ]
        return sorted(
            eligible,
            key=lambda assessment: (
                _priority_value(assessment.priority),
                assessment.issue.created_at,
                assessment.issue.number,
            ),
        )


def _priority_value(priority: Priority) -> int:
    return 4 if priority == "none" else int(priority[1])


def _assessment(
    issue: GitHubIssueCandidate,
    decision: str,
    reason: str,
    priority: Priority,
) -> CandidateAssessment:
    return CandidateAssessment(
        issue=issue,
        issue_number=issue.number,
        decision=decision,
        reason=reason,
        priority=priority,
    )
