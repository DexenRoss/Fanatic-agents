"""Network-free provenance, integrity, and Git delivery tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from fanatic_agents.core.config import PermissionsConfig
from fanatic_agents.delivery.models import DeliveryResult, PromotionReceipt
from fanatic_agents.delivery.receipt import (
    PromotionReceiptStore,
    receipt_path_for_worktree,
)
from fanatic_agents.delivery.service import DeliveryService
from fanatic_agents.git.promotion import (
    VerifiedChangePromotionService,
    capture_base_repository_state,
)
from fanatic_agents.git.worktree import GitRunner
from fanatic_agents.github.client import GitHubCommandError, GitHubPreflight, PullRequestReference
from fanatic_agents.implementation.models import (
    ChangeOperation,
    ChangeSet,
    ImplementationResult,
    changeset_sha256,
)
from fanatic_agents.sandbox.models import SandboxCommandResult


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, capture_output=True, check=check,
        shell=False, text=True, timeout=5,
    )


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git(root, "init", "-b", "base")
    git(root, "config", "user.email", "delivery-tests@example.invalid")
    git(root, "config", "user.name", "Fanatic Delivery Tests")
    (root / "modify.txt").write_text("original\n", encoding="utf-8")
    (root / "delete.txt").write_text("delete\n", encoding="utf-8")
    git(root, "add", "modify.txt", "delete.txt")
    git(root, "commit", "-m", "base")
    git(root, "remote", "add", "origin", "https://github.com/example/project.git")
    return root


def change_set() -> ChangeSet:
    return ChangeSet(
        task_title="Deliver exact verified files",
        summary="Bounded delivery fixture",
        changes=[
            ChangeOperation(
                operation="modify", path="modify.txt", content="verified\n", reason="task"
            ),
            ChangeOperation(
                operation="create", path="created.txt", content="created\n", reason="task"
            ),
            ChangeOperation(
                operation="delete", path="delete.txt", content=None, reason="task"
            ),
        ],
    )


def promote(root: Path) -> tuple[Path, PromotionReceipt]:
    selected = change_set()
    verification = SandboxCommandResult(
        argv=["python", "-m", "pytest"],
        exit_code=0,
        stdout="not persisted",
        stderr="",
        duration_seconds=0.1,
        timed_out=False,
        stdout_truncated=False,
        stderr_truncated=False,
    )
    implementation = ImplementationResult(
        task=selected.task_title,
        base_repository_state=capture_base_repository_state(root),
        changeset=selected,
        verified_changeset_sha256=changeset_sha256(selected),
        verification_results=[verification],
        status="verified",
        tests_passed=True,
        commands_executed_count=1,
    )
    result = VerifiedChangePromotionService().promote(
        repository=root,
        implementation=implementation,
        branch="fanatic/delivery-test",
        files_likely_affected=["modify.txt", "created.txt", "delete.txt"],
    )
    assert result.status == "promoted" and result.worktree_path
    worktree = Path(result.worktree_path)
    return worktree, PromotionReceiptStore().load(worktree)


class NetworklessGit(GitRunner):
    def __init__(
        self, *, remote_sha: str = "", push_returncode: int = 0, stage_returncode: int = 0
    ) -> None:
        super().__init__()
        self.remote_sha = remote_sha
        self.push_returncode = push_returncode
        self.stage_returncode = stage_returncode
        self.calls: list[tuple[str, ...]] = []

    def run(self, repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
        self.calls.append(arguments)
        if arguments[:1] == ("ls-remote",):
            output = (
                f"{self.remote_sha}\trefs/heads/fanatic/delivery-test\n"
                if self.remote_sha
                else ""
            )
            return subprocess.CompletedProcess(["git", *arguments], 0, output, "")
        if arguments[:1] == ("push",):
            return subprocess.CompletedProcess(
                ["git", *arguments], self.push_returncode, "", "synthetic failure"
            )
        if arguments[:3] == ("add", "--all", "--") and self.stage_returncode:
            return subprocess.CompletedProcess(
                ["git", *arguments], self.stage_returncode, "", "synthetic failure"
            )
        return super().run(repository, *arguments)


class FakeGitHub:
    def __init__(
        self,
        *,
        preflight: str = "ok",
        existing: PullRequestReference | None = None,
        fail_create: bool = False,
    ) -> None:
        self.preflight_status = preflight
        self.existing = existing
        self.fail_create = fail_create
        self.find_calls = 0
        self.create_calls = 0
        self.created: dict[str, str] = {}

    def preflight(self) -> GitHubPreflight:
        return GitHubPreflight(self.preflight_status)  # type: ignore[arg-type]

    def find_pull_request(self, repository: str, *, base: str, head: str):
        self.find_calls += 1
        self.created.update(repository=repository, base=base, head=head)
        return self.existing

    def create_pull_request(
        self, repository: str, *, base: str, head: str, title: str, body: str
    ) -> PullRequestReference:
        self.create_calls += 1
        self.created.update(
            repository=repository, base=base, head=head, title=title, body=body
        )
        if self.fail_create:
            raise GitHubCommandError("synthetic")
        return PullRequestReference(42, "https://github.com/example/project/pull/42")


def service(
    *, git_runner: NetworklessGit | None = None, github: FakeGitHub | None = None
) -> DeliveryService:
    return DeliveryService(git=git_runner or NetworklessGit(), github=github or FakeGitHub())


def allow_all() -> PermissionsConfig:
    return PermissionsConfig(commit=True, push_branch=True, create_pull_request=True)


def update_receipt(worktree: Path, **updates: object) -> PromotionReceipt:
    store = PromotionReceiptStore()
    values = store.load(worktree).model_dump()
    values.update(updates)
    receipt = PromotionReceipt.model_validate(values)
    store.save(receipt)
    return receipt


def test_receipt_is_external_strict_and_secret_free(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    path = receipt_path_for_worktree(worktree)
    assert path.parent.name == ".metadata"
    assert not path.is_relative_to(worktree)
    assert {change.operation for change in receipt.expected_changes} == {
        "create", "modify", "delete"
    }
    assert all(
        change.content_sha256 is None or len(change.content_sha256) == 64
        for change in receipt.expected_changes
    )
    raw = path.read_text(encoding="utf-8")
    assert "not persisted" not in raw
    with pytest.raises(ValidationError):
        PromotionReceipt.model_validate({**receipt.model_dump(), "unexpected": True})


def test_missing_and_corrupt_receipts_are_rejected(tmp_path: Path) -> None:
    missing = tmp_path / ".fanatic-agents-worktrees" / "project" / "missing"
    missing.mkdir(parents=True)
    assert service().deliver(missing).status == "invalid_promotion"

    root = repository(tmp_path / "corrupt")
    worktree, _ = promote(root)
    receipt_path_for_worktree(worktree).write_text("{not-json", encoding="utf-8")
    assert service().deliver(worktree).status == "invalid_promotion"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"implementation_status": "failed"}, "invalid_promotion"),
        ({"promotion_status": "pending"}, "invalid_promotion"),
    ],
)
def test_invalid_receipt_statuses_fail_closed(
    tmp_path: Path, updates: dict[str, object], expected: str
) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    payload = receipt.model_dump(mode="json")
    payload.update(updates)
    receipt_path_for_worktree(worktree).write_text(json.dumps(payload), encoding="utf-8")
    assert service().deliver(worktree).status == expected


def test_wrong_worktree_repository_base_and_branch_are_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    wrong = worktree.parent / "wrong"
    wrong.mkdir()
    receipt_path_for_worktree(wrong).write_text(receipt.model_dump_json(), encoding="utf-8")
    assert service().deliver(wrong).status == "invalid_promotion"

    update_receipt(worktree, base_commit="f" * 40)
    assert service().deliver(worktree).status == "invalid_promotion"
    PromotionReceiptStore().save(receipt)
    git(worktree, "checkout", "-b", "fanatic/other")
    assert service().deliver(worktree).status == "invalid_promotion"


@pytest.mark.parametrize("mutation", ["content", "extra", "missing", "restored_delete"])
def test_human_changes_after_promotion_are_rejected(tmp_path: Path, mutation: str) -> None:
    root = repository(tmp_path)
    worktree, _ = promote(root)
    if mutation == "content":
        (worktree / "modify.txt").write_text("human\n", encoding="utf-8")
    elif mutation == "extra":
        (worktree / "extra.log").write_text("artifact\n", encoding="utf-8")
    elif mutation == "missing":
        (worktree / "created.txt").unlink()
    else:
        (worktree / "delete.txt").write_text("restored\n", encoding="utf-8")
    assert service().deliver(worktree).status == "modified_after_verification"


def test_check_accepts_exact_content_without_side_effects(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    runner = NetworklessGit()
    result = service(git_runner=runner).deliver(
        worktree, permissions=allow_all(), check_only=True
    )
    assert result.status == "ready" and result.commit_sha is None
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit
    assert not any(call[:1] in {("add",), ("commit",), ("push",)} for call in runner.calls)


def test_success_stages_exact_paths_commits_pushes_and_creates_pr(tmp_path: Path) -> None:
    root = repository(tmp_path)
    source_head = git(root, "rev-parse", "HEAD").stdout.strip()
    worktree, receipt = promote(root)
    runner = NetworklessGit()
    github = FakeGitHub()
    result = service(git_runner=runner, github=github).deliver(
        worktree, permissions=allow_all()
    )
    assert result.status == "delivered" and result.final_status == "DELIVERED_FOR_REVIEW"
    assert result.pr_number == 42 and result.pr_url and result.commit_sha
    add = next(call for call in runner.calls if call[:1] == ("add",))
    assert add[:3] == ("add", "--all", "--")
    assert set(add[3:]) == {change.path for change in receipt.expected_changes}
    commit = next(call for call in runner.calls if call[:1] == ("commit",))
    assert commit == ("commit", "-m", "fanatic: Deliver exact verified files")
    push = next(call for call in runner.calls if call[:1] == ("push",))
    assert push == ("push", "--set-upstream", "origin", "fanatic/delivery-test")
    flat = [argument for call in runner.calls for argument in call]
    assert "--force" not in flat and "--force-with-lease" not in flat
    assert not any(call[:1] in {("merge",), ("rebase",)} for call in runner.calls)
    assert github.created["base"] == "base"
    assert github.created["head"] == "fanatic/delivery-test"
    assert "VERIFIED" in github.created["body"]
    assert "no automatic merge" in github.created["body"]
    assert git(worktree, "rev-parse", "HEAD^").stdout.strip() == source_head
    assert git(root, "rev-parse", "HEAD").stdout.strip() == source_head
    assert git(root, "status", "--porcelain").stdout == ""


def test_stage_and_commit_failures_stop_before_push(tmp_path: Path) -> None:
    root = repository(tmp_path / "stage")
    worktree, _ = promote(root)
    stage_runner = NetworklessGit(stage_returncode=1)
    staged = service(git_runner=stage_runner).deliver(worktree)
    assert staged.status == "staging_failed"
    assert not any(call[:1] == ("push",) for call in stage_runner.calls)

    root = repository(tmp_path / "hook")
    worktree, receipt = promote(root)
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    commit_runner = NetworklessGit()
    committed = service(git_runner=commit_runner).deliver(worktree)
    assert committed.status == "commit_failed"
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit
    assert not any(call[:1] == ("push",) for call in commit_runner.calls)
    assert not any("--no-verify" in call for call in commit_runner.calls)


def test_remote_exists_and_push_failure_preserve_one_local_commit(tmp_path: Path) -> None:
    root = repository(tmp_path / "exists")
    worktree, _ = promote(root)
    exists_runner = NetworklessGit(remote_sha="a" * 40)
    exists = service(git_runner=exists_runner).deliver(worktree)
    assert exists.status == "remote_branch_exists" and exists.commit_sha
    assert not any(call[:1] == ("push",) for call in exists_runner.calls)

    root = repository(tmp_path / "failure")
    worktree, _ = promote(root)
    failed_runner = NetworklessGit(push_returncode=1)
    failed = service(git_runner=failed_runner).deliver(worktree)
    assert failed.status == "push_failed" and failed.commit_sha
    assert git(worktree, "rev-list", "--count", "HEAD^..HEAD").stdout.strip() == "1"
    retried = service().deliver(worktree)
    assert retried.status == "delivered" and retried.commit_sha == failed.commit_sha
    assert git(worktree, "rev-list", "--count", "HEAD^..HEAD").stdout.strip() == "1"


def test_pr_failure_is_retried_without_duplicate_commit_or_pr(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, _ = promote(root)
    failing = FakeGitHub(fail_create=True)
    first = service(github=failing).deliver(worktree)
    assert first.status == "pr_creation_failed" and first.commit_sha
    assert PromotionReceiptStore().load(worktree).delivery_stage == "branch_pushed"

    reference = PullRequestReference(77, "https://github.com/example/project/pull/77")
    existing = FakeGitHub(existing=reference)
    second_runner = NetworklessGit()
    second = service(git_runner=second_runner, github=existing).deliver(worktree)
    assert second.status == "delivered" and second.pr_number == 77
    assert existing.find_calls == 1 and existing.create_calls == 0
    assert not any(call[:1] in {("commit",), ("push",)} for call in second_runner.calls)


@pytest.mark.parametrize(
    "permissions",
    [
        PermissionsConfig(commit=False, push_branch=True, create_pull_request=True),
        PermissionsConfig(commit=True, push_branch=False, create_pull_request=True),
        PermissionsConfig(commit=True, push_branch=True, create_pull_request=False),
    ],
)
def test_delivery_permissions_are_deny_by_default(
    tmp_path: Path, permissions: PermissionsConfig
) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    result = service().deliver(worktree, permissions=permissions)
    assert result.status == "permission_denied"
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit


@pytest.mark.parametrize(
    ("preflight", "status"),
    [("not_found", "github_cli_unavailable"), ("not_authenticated", "github_auth_required")],
)
def test_github_preflight_stops_before_commit(
    tmp_path: Path, preflight: str, status: str
) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    result = service(github=FakeGitHub(preflight=preflight)).deliver(worktree)
    assert result.status == status
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit


def test_result_and_subject_validation_are_structured(tmp_path: Path) -> None:
    result = DeliveryResult(
        repository="/repo", worktree_path="/worktree", status="push_failed"
    )
    assert result.final_status == "PUSH_FAILED"
    with pytest.raises(ValidationError):
        DeliveryResult(repository="/repo", worktree_path="/worktree", status="merged")

    root = repository(tmp_path)
    worktree, receipt = promote(root)
    rejected = service().deliver(worktree, commit_message="bad\nsubject")
    assert rejected.status == "delivery_failed"
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit
