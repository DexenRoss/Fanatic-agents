"""Deterministic Sprint 7 pull request observation tests."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest

from fanatic_agents.core.config import PermissionsConfig
from fanatic_agents.delivery.models import ExpectedPromotedChange, PromotionReceipt
from fanatic_agents.delivery.receipt import PromotionReceiptStore, repository_identifier
from fanatic_agents.github.client import GitHubPreflight
from fanatic_agents.observation.receipt import PullRequestObservationStore
from fanatic_agents.observation.service import PullRequestObservationService

COMMIT = "b" * 40


class FakeGitHub:
    def __init__(
        self,
        responses: list[dict[str, object]],
        *,
        preflight: str = "ok",
    ) -> None:
        self.responses = responses
        self.preflight_status = preflight
        self.calls: list[tuple[str, int]] = []

    def preflight(self) -> GitHubPreflight:
        return GitHubPreflight(self.preflight_status)  # type: ignore[arg-type]

    def view_pull_request(self, repository: str, number: int) -> dict[str, object]:
        self.calls.append((repository, number))
        return deepcopy(self.responses.pop(0))


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def delivered_worktree(tmp_path: Path) -> tuple[Path, PromotionReceipt]:
    repository = tmp_path / "repository"
    repository.mkdir(parents=True)
    worktree = (
        tmp_path
        / ".fanatic-agents-worktrees"
        / "repository"
        / "fanatic-task"
    )
    worktree.mkdir(parents=True)
    now = datetime.now(UTC)
    receipt = PromotionReceipt(
        repository_id=repository_identifier(repository),
        repository_path=str(repository),
        base_branch="main",
        base_commit="a" * 40,
        promoted_branch="fanatic/task",
        worktree_path=str(worktree),
        task_title="Task",
        expected_changes=[
            ExpectedPromotedChange(
                path="file.py", operation="create", content_sha256="c" * 64
            )
        ],
        implementation_status="verified",
        promotion_status="promoted",
        created_at=now,
        updated_at=now,
        delivery_stage="pr_created",
        commit_sha=COMMIT,
        remote="origin",
        remote_branch="fanatic/task",
        pr_number=42,
        pr_url="https://github.com/owner/repo/pull/42",
    )
    PromotionReceiptStore().save(receipt)
    return worktree, receipt


def payload(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "number": 42,
        "url": "https://github.com/owner/repo/pull/42",
        "state": "OPEN",
        "isDraft": False,
        "baseRefName": "main",
        "headRefName": "fanatic/task",
        "headRefOid": COMMIT,
        "mergeable": "MERGEABLE",
        "reviewDecision": "APPROVED",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "test",
                "workflowName": "CI",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://github.com/owner/repo/actions/runs/1",
            }
        ],
        "reviews": [
            {"author": {"login": "reviewer"}, "state": "APPROVED"}
        ],
        "mergedAt": None,
        "closedAt": None,
    }
    result.update(updates)
    return result


def observe(
    tmp_path: Path, response: dict[str, object]
) -> tuple[object, Path, FakeGitHub]:
    worktree, _ = delivered_worktree(tmp_path)
    github = FakeGitHub([response])
    result = PullRequestObservationService(github=github).observe_once(worktree)
    return result, worktree, github


def check_run(status: str, conclusion: str | None = None, name: str = "test") -> dict[str, object]:
    return {
        "__typename": "CheckRun",
        "name": name,
        "workflowName": "CI",
        "status": status,
        "conclusion": conclusion,
    }


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({}, "ready_for_human_merge"),
        ({"isDraft": True}, "pr_draft"),
        ({"state": "CLOSED"}, "pr_closed"),
        ({"state": "MERGED", "mergedAt": "2026-08-18T00:00:00Z"}, "merged_externally"),
        ({"mergeable": "CONFLICTING"}, "merge_conflict"),
        ({"headRefOid": "d" * 40}, "pr_head_drifted"),
    ],
)
def test_pr_terminal_states_and_precedence(
    tmp_path: Path, updates: dict[str, object], expected: str
) -> None:
    result, _, _ = observe(tmp_path, payload(**updates))
    assert result.status == expected


@pytest.mark.parametrize(
    "updates",
    [
        {"number": 43},
        {"url": "https://github.com/other/repo/pull/42"},
        {"baseRefName": "develop"},
        {"headRefName": "fanatic/other"},
    ],
)
def test_wrong_pr_repository_or_branches_fail_provenance(
    tmp_path: Path, updates: dict[str, object]
) -> None:
    result, _, _ = observe(tmp_path, payload(**updates))
    assert result.status == "invalid_delivery"


@pytest.mark.parametrize(
    ("checks", "ci_state", "status"),
    [
        ([check_run("COMPLETED", "SUCCESS")], "passed", "ready_for_human_merge"),
        ([check_run("IN_PROGRESS")], "pending", "waiting_for_ci"),
        ([check_run("COMPLETED", "FAILURE")], "failed", "ci_failed"),
        ([check_run("COMPLETED", "CANCELLED")], "failed", "ci_failed"),
        (
            [check_run("COMPLETED", "SUCCESS"), check_run("QUEUED", name="lint")],
            "pending",
            "waiting_for_ci",
        ),
        (
            [check_run("COMPLETED", "SUCCESS"), check_run("COMPLETED", "FAILURE", "lint")],
            "failed",
            "ci_failed",
        ),
        ([], "no_ci_reported", "no_ci_reported"),
        ([check_run("FUTURE_STATE", "FUTURE_RESULT")], "pending", "waiting_for_ci"),
        (
            [
                check_run("COMPLETED", "SUCCESS"),
                {
                    "__typename": "StatusContext",
                    "context": "GitGuardian",
                    "state": "SUCCESS",
                    "targetUrl": "https://example.invalid/check",
                },
            ],
            "passed",
            "ready_for_human_merge",
        ),
    ],
)
def test_ci_aggregation_is_conservative_across_providers(
    tmp_path: Path,
    checks: list[dict[str, object]],
    ci_state: str,
    status: str,
) -> None:
    result, _, _ = observe(tmp_path, payload(statusCheckRollup=checks))
    assert result.ci_state == ci_state
    assert result.status == status
    assert [check.name for check in result.checks] == [
        str(check.get("name") or check.get("context")) for check in checks
    ]


@pytest.mark.parametrize(
    ("decision", "expected_review", "expected_status"),
    [
        ("APPROVED", "approved", "ready_for_human_merge"),
        ("CHANGES_REQUESTED", "changes_requested", "changes_requested"),
        ("REVIEW_REQUIRED", "review_required", "waiting_for_review"),
        (None, "none", "waiting_for_review"),
        ("FUTURE_DECISION", "unknown", "waiting_for_review"),
    ],
)
def test_review_decisions_are_normalized(
    tmp_path: Path,
    decision: str | None,
    expected_review: str,
    expected_status: str,
) -> None:
    result, _, _ = observe(tmp_path, payload(reviewDecision=decision, reviews=[]))
    assert result.review_state == expected_review
    assert result.status == expected_status


def test_latest_reviews_are_counted_without_storing_full_payload(tmp_path: Path) -> None:
    reviews = [
        {"author": {"login": "alice"}, "state": "CHANGES_REQUESTED"},
        {"author": {"login": "alice"}, "state": "APPROVED"},
        {"author": {"login": "bob"}, "state": "APPROVED"},
    ]
    result, worktree, _ = observe(tmp_path, payload(reviews=reviews))
    assert result.approvals == 2 and result.changes_requested == 0
    persisted = PullRequestObservationStore().load(worktree)
    assert persisted == result
    assert "reviews" not in persisted.model_dump()


def test_invalid_receipt_and_permission_denial_never_query_github(tmp_path: Path) -> None:
    worktree = tmp_path / ".fanatic-agents-worktrees" / "repo" / "missing"
    github = FakeGitHub([payload()])
    service = PullRequestObservationService(github=github)
    assert service.observe_once(worktree).status == "invalid_delivery"
    assert github.calls == []

    worktree, receipt = delivered_worktree(tmp_path / "valid")
    denied = service.observe_once(
        worktree,
        permissions=PermissionsConfig(),
        configured_repository=Path(receipt.repository_path),
    )
    assert denied.status == "invalid_delivery"
    assert github.calls == []


def test_github_unavailable_and_unexpected_schema_fail_closed(tmp_path: Path) -> None:
    worktree, _ = delivered_worktree(tmp_path)
    missing = PullRequestObservationService(
        github=FakeGitHub([payload()], preflight="not_found")
    ).observe_once(worktree)
    assert missing.status == "github_unavailable"

    malformed = PullRequestObservationService(
        github=FakeGitHub([payload(isDraft="false")])
    ).observe_once(worktree)
    assert malformed.status == "observation_failed"


def test_observation_does_not_modify_source_or_worktree(tmp_path: Path) -> None:
    worktree, receipt = delivered_worktree(tmp_path)
    source = Path(receipt.repository_path)
    (source / "source.txt").write_text("source\n", encoding="utf-8")
    (worktree / "worktree.txt").write_text("worktree\n", encoding="utf-8")
    before_source = (source / "source.txt").read_bytes()
    before_worktree = (worktree / "worktree.txt").read_bytes()

    result = PullRequestObservationService(github=FakeGitHub([payload()])).observe_once(
        worktree
    )

    assert result.status == "ready_for_human_merge"
    assert (source / "source.txt").read_bytes() == before_source
    assert (worktree / "worktree.txt").read_bytes() == before_worktree
    assert list(source.iterdir()) == [source / "source.txt"]
    assert list(worktree.iterdir()) == [worktree / "worktree.txt"]


@pytest.mark.parametrize(
    ("responses", "terminal"),
    [
        (
            [payload(statusCheckRollup=[check_run("QUEUED")]), payload()],
            "ready_for_human_merge",
        ),
        (
            [
                payload(statusCheckRollup=[check_run("QUEUED")]),
                payload(statusCheckRollup=[check_run("COMPLETED", "FAILURE")]),
            ],
            "ci_failed",
        ),
        (
            [
                payload(statusCheckRollup=[check_run("QUEUED")]),
                payload(reviewDecision="CHANGES_REQUESTED"),
            ],
            "changes_requested",
        ),
    ],
)
def test_bounded_watch_stops_on_terminal_transitions(
    tmp_path: Path,
    responses: list[dict[str, object]],
    terminal: str,
) -> None:
    worktree, _ = delivered_worktree(tmp_path)
    clock = FakeClock()
    github = FakeGitHub(responses)
    result = PullRequestObservationService(github=github, clock=clock).observe_until_terminal(
        worktree, interval_seconds=10, timeout_seconds=30
    )
    assert result.status == terminal
    assert clock.sleeps == [10]
    assert len(github.calls) == 2


def test_watch_timeout_returns_last_pending_snapshot_without_infinite_polling(
    tmp_path: Path,
) -> None:
    worktree, _ = delivered_worktree(tmp_path)
    pending = payload(statusCheckRollup=[check_run("IN_PROGRESS")])
    clock = FakeClock()
    github = FakeGitHub([pending, pending, pending])
    result = PullRequestObservationService(github=github, clock=clock).observe_until_terminal(
        worktree, interval_seconds=10, timeout_seconds=20
    )
    assert result.status == "waiting_for_ci"
    assert result.stop_reason and "timeout" in result.stop_reason.lower()
    assert clock.sleeps == [10, 10]
    assert len(github.calls) == 3


def test_watch_rejects_unbounded_or_tight_polling(tmp_path: Path) -> None:
    worktree, _ = delivered_worktree(tmp_path)
    service = PullRequestObservationService(github=FakeGitHub([payload()]))
    with pytest.raises(ValueError, match="at least 10"):
        service.observe_until_terminal(worktree, interval_seconds=9, timeout_seconds=20)
    with pytest.raises(ValueError, match="1800"):
        service.observe_until_terminal(worktree, interval_seconds=10, timeout_seconds=1801)
