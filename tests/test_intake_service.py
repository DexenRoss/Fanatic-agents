"""Service, receipt, bounds, and read-only guarantees for Sprint 8."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from fanatic_agents.core.config import IntakeConfig, PermissionsConfig
from fanatic_agents.github.client import GitHubPreflight
from fanatic_agents.intake.receipt import TaskIntakeReceiptStore
from fanatic_agents.intake.service import TaskIntakeService


class FakeGit:
    def __init__(self, root: Path, *, origin: str = "git@github.com:owner/repo.git"):
        self.root = root
        self.origin = origin
        self.calls: list[tuple[str, ...]] = []

    def run(self, repository: Path, *arguments: str):
        self.calls.append(arguments)
        values = {
            ("rev-parse", "--is-inside-work-tree"): (0, "true\n"),
            ("rev-parse", "--show-toplevel"): (0, f"{self.root}\n"),
            ("symbolic-ref", "--quiet", "--short", "HEAD"): (0, "feature/base\n"),
            ("rev-parse", "--verify", "HEAD"): (0, f"{'a' * 40}\n"),
            ("remote", "get-url", "origin"): (0, f"{self.origin}\n"),
        }
        code, stdout = values.get(arguments, (2, ""))
        return subprocess.CompletedProcess(["git", *arguments], code, stdout, "")


class FakeGitHub:
    def __init__(
        self,
        payloads: list[dict[str, object]],
        *,
        preflight: str = "ok",
    ):
        self.payloads = payloads
        self.preflight_status = preflight
        self.calls: list[tuple[str, object]] = []

    def preflight(self) -> GitHubPreflight:
        self.calls.append(("preflight", None))
        return GitHubPreflight(self.preflight_status)

    def list_open_issues(self, repository: str, *, limit: int):
        self.calls.append(("list_open_issues", (repository, limit)))
        return list(self.payloads)


def payload(
    number: int,
    *,
    labels: list[str] | None = None,
    state: str = "OPEN",
    created: str = "2025-01-01T00:00:00Z",
    body: str = "description",
) -> dict[str, object]:
    return {
        "number": number,
        "title": f"Task {number}",
        "body": body,
        "url": f"https://github.com/owner/repo/issues/{number}",
        "state": state,
        "labels": [{"name": item} for item in (labels or ["fanatic:ready"])],
        "assignees": [],
        "author": None,
        "createdAt": created,
        "updatedAt": created,
        "milestone": None,
    }


def service(
    repository: Path,
    payloads: list[dict[str, object]],
    metadata: Path,
    *,
    origin: str = "git@github.com:owner/repo.git",
) -> tuple[TaskIntakeService, FakeGit, FakeGitHub]:
    git = FakeGit(repository, origin=origin)
    github = FakeGitHub(payloads)
    intake = TaskIntakeService(
        git=git,
        github=github,
        receipts=TaskIntakeReceiptStore(metadata_root=metadata),
        clock=lambda: datetime(2025, 2, 3, tzinfo=UTC),
    )
    return intake, git, github


def test_discovery_lists_only_eligible_without_writing_receipt(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "metadata"
    intake, git, github = service(
        repository,
        [
            payload(1),
            payload(2, labels=["bug"]),
            payload(3, labels=["fanatic:ready", "fanatic:blocked"]),
        ],
        metadata,
    )

    result = intake.discover(repository)

    assert result.status == "tasks_discovered"
    assert result.candidates_fetched == 3
    assert [item.number for item in result.eligible_candidates] == [1]
    assert not metadata.exists()
    assert github.calls[-1] == ("list_open_issues", ("owner/repo", 50))
    assert_read_only_calls(git, github)


def test_select_ranks_one_task_and_persists_exact_external_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "metadata"
    monkeypatch.setenv("OPENAI_API_KEY", "never-store-this-secret")
    intake, git, github = service(
        repository,
        [
            payload(9, labels=["fanatic:ready", "priority:p2"]),
            payload(7, labels=["fanatic:ready", "priority:p0"]),
        ],
        metadata,
    )

    result = intake.select(repository)

    assert result.status == "task_selected"
    assert result.selected_task is not None
    assert result.selected_task.issue_number == 7
    assert result.selected_task.base_branch == "feature/base"
    assert result.selected_task.base_commit_sha == "a" * 40
    receipt_path = Path(result.receipt_path or "")
    assert receipt_path.is_file()
    assert not receipt_path.is_relative_to(repository)
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert receipt["issue_number"] == 7
    assert receipt["base_commit_sha"] == "a" * 40
    assert receipt["task_status"] == "selected"
    assert "body" not in receipt and "description" not in receipt
    assert "never-store-this-secret" not in receipt_path.read_text(encoding="utf-8")
    assert list(repository.iterdir()) == []
    assert_read_only_calls(git, github)


def test_duplicate_active_issue_is_not_selected_twice(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, _ = service(repository, [payload(1)], tmp_path / "metadata")

    first = intake.select(repository)
    second = intake.select(repository)

    assert first.status == "task_selected"
    assert second.status == "no_eligible_tasks"
    assert second.selected_task is None


def test_empty_issue_list_is_a_normal_terminal_result(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, _ = service(repository, [], tmp_path / "metadata")

    discovered = intake.discover(repository)

    assert discovered.status == "no_eligible_tasks"
    assert discovered.candidates_fetched == 0


def test_corrupt_receipt_fails_closed(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    metadata = tmp_path / "metadata"
    intake, _, _ = service(repository, [payload(1)], metadata)
    store = TaskIntakeReceiptStore(metadata_root=metadata)
    directory = store.directory(repository)
    directory.mkdir(parents=True)
    (directory / "issue-99.json").write_text("{not-json", encoding="utf-8")

    result = intake.select(repository)

    assert result.status == "intake_failed"
    assert result.selected_task is None
    assert "invalid" in (result.stop_reason or "").lower()
    assert (directory / "issue-99.json").exists()


def test_no_eligible_and_ambiguous_priority_are_structured_results(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, _ = service(repository, [payload(1, labels=["bug"])], tmp_path / "m1")
    assert intake.select(repository).status == "no_eligible_tasks"

    intake, _, _ = service(
        repository,
        [payload(2, labels=["fanatic:ready", "priority:p0", "priority:p3"])],
        tmp_path / "m2",
    )
    assert intake.select(repository).status == "ambiguous_priority"


def test_config_is_deny_by_default_and_manual_invocation_is_authorized(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, github = service(repository, [payload(1)], tmp_path / "metadata")

    disabled = intake.discover(
        repository,
        intake_config=IntakeConfig(),
        permissions=PermissionsConfig(read_issues=True),
    )
    denied = intake.discover(
        repository,
        intake_config=IntakeConfig(enabled=True),
        permissions=PermissionsConfig(),
    )
    manual = intake.discover(repository)

    assert disabled.status == "intake_disabled"
    assert denied.status == "invalid_configuration"
    assert manual.status == "tasks_discovered"
    assert [call[0] for call in github.calls].count("list_open_issues") == 1


def test_candidate_window_is_bounded_and_locally_sliced(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, github = service(
        repository, [payload(i) for i in range(1, 8)], tmp_path / "metadata"
    )

    result = intake.discover(
        repository,
        intake_config=IntakeConfig(enabled=True, max_candidates=3),
        permissions=PermissionsConfig(read_issues=True),
    )

    assert result.candidates_fetched == 3
    assert github.calls[-1] == ("list_open_issues", ("owner/repo", 3))


def test_select_always_uses_fresh_issue_snapshot(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, github = service(repository, [payload(1)], tmp_path / "metadata")

    assert intake.discover(repository).candidates_eligible == 1
    github.payloads = [payload(1, state="CLOSED")]
    selected = intake.select(repository)

    assert selected.status == "no_eligible_tasks"


def test_invalid_origin_and_github_preflight_fail_safely(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    intake, _, _ = service(
        repository,
        [payload(1)],
        tmp_path / "metadata",
        origin="https://gitlab.com/owner/repo.git",
    )
    assert intake.discover(repository).status == "invalid_repository"

    git = FakeGit(repository)
    github = FakeGitHub([payload(1)], preflight="not_authenticated")
    intake = TaskIntakeService(
        git=git,
        github=github,
        receipts=TaskIntakeReceiptStore(metadata_root=tmp_path / "other"),
    )
    assert intake.discover(repository).status == "github_unavailable"


def test_https_and_ssh_origins_are_resolved(tmp_path: Path) -> None:
    for index, origin in enumerate(
        [
            "https://github.com/owner/repo.git",
            "git@github.com:owner/repo.git",
        ]
    ):
        repository = tmp_path / f"repo-{index}"
        repository.mkdir()
        intake, _, _ = service(
            repository, [payload(1)], tmp_path / f"metadata-{index}", origin=origin
        )
        assert intake.discover(repository).github_repository == "owner/repo"


def assert_read_only_calls(git: FakeGit, github: FakeGitHub) -> None:
    forbidden_git = {"add", "commit", "push", "merge", "checkout", "switch"}
    assert all(call[0] not in forbidden_git for call in git.calls)
    assert all(call[0] in {"preflight", "list_open_issues"} for call in github.calls)
