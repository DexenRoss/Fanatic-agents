"""Real-Git tests for explicit verified ChangeSet promotion."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from fanatic_agents.git.errors import RepositoryStateError
from fanatic_agents.git.models import PromotionResult
from fanatic_agents.git.promotion import (
    VerifiedChangePromotionService,
    branch_policy_allows,
    capture_base_repository_state,
    promotion_worktree_path,
)
from fanatic_agents.implementation.apply import ChangeSetApplier
from fanatic_agents.implementation.models import (
    ChangeOperation,
    ChangeSet,
    ImplementationResult,
    changeset_sha256,
)
from fanatic_agents.implementation.policy import (
    ChangePolicyIssue,
    ChangePolicyResult,
)


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    if arguments[:2] == ("show-ref", "--verify") and "--quiet" not in arguments:
        arguments = (*arguments[:2], "--quiet", *arguments[2:])
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        check=check,
        shell=False,
        text=True,
        timeout=5,
    )


def repository(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir(parents=True)
    git(root, "init", "-b", "base")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "user.name", "Fanatic Tests")
    (root / "modify.txt").write_text("original\n", encoding="utf-8")
    (root / "delete.txt").write_text("delete me\n", encoding="utf-8")
    git(root, "add", "modify.txt", "delete.txt")
    git(root, "commit", "-m", "base")
    return root


def changeset() -> ChangeSet:
    return ChangeSet(
        task_title="Promote verified files",
        summary="Create, modify, and delete exact content.",
        changes=[
            ChangeOperation(
                operation="modify",
                path="modify.txt",
                content="verified modification\n",
                reason="task",
            ),
            ChangeOperation(
                operation="create",
                path="created.txt",
                content="verified creation\n",
                reason="task",
            ),
            ChangeOperation(
                operation="delete",
                path="delete.txt",
                content=None,
                reason="task",
            ),
        ],
    )


def implementation(
    root: Path,
    *,
    status: str = "verified",
    selected_changeset: ChangeSet | None = None,
) -> ImplementationResult:
    selected = selected_changeset or changeset()
    return ImplementationResult(
        task="Promote verified files",
        base_repository_state=capture_base_repository_state(root),
        changeset=selected,
        verified_changeset_sha256=(
            changeset_sha256(selected) if status == "verified" else None
        ),
        status=status,
        tests_passed=status == "verified",
        commands_executed_count=1,
    )  # type: ignore[arg-type]


def promote(root: Path, result: ImplementationResult, branch: str = "fanatic/task"):
    return VerifiedChangePromotionService().promote(
        repository=root,
        implementation=result,
        branch=branch,
        files_likely_affected=["modify.txt", "created.txt", "delete.txt"],
    )


def test_promotion_result_is_strict_and_structured() -> None:
    result = PromotionResult(
        repository="/project",
        base_branch="base",
        base_commit="abc",
        promoted_branch="fanatic/task",
        worktree_path="/worktree",
        changes=3,
        status="promoted",
    )
    assert result.status == "promoted" and result.changes == 3
    with pytest.raises(ValidationError):
        PromotionResult(repository="/project", changes="3", status="promoted")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        PromotionResult(repository="/project", status="promoted", secret="x")  # type: ignore[call-arg]


@pytest.mark.parametrize("status", ["verification_failed", "human_required", "policy_rejected"])
def test_only_verified_implementation_can_promote(tmp_path: Path, status: str) -> None:
    root = repository(tmp_path)
    result = implementation(root, status=status)
    promotion = promote(root, result)
    assert promotion.status == "not_verified"
    assert git(root, "show-ref", "--verify", "refs/heads/fanatic/task", check=False).returncode == 1
    assert not promotion_worktree_path(root, "fanatic/task").exists()


def test_repository_must_be_git_and_detached_head_is_rejected(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(RepositoryStateError) as invalid:
        capture_base_repository_state(plain)
    assert invalid.value.status == "repository_invalid"

    root = repository(tmp_path / "detached")
    git(root, "checkout", "--detach")
    with pytest.raises(RepositoryStateError) as detached:
        capture_base_repository_state(root)
    assert detached.value.status == "detached_head"


def test_changeset_modified_after_verification_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    result = implementation(root)
    assert result.changeset is not None
    result.changeset.changes[0].content = "tampered\n"
    promotion = promote(root, result)
    assert promotion.status == "promotion_failed"
    assert "no longer matches" in (promotion.stop_reason or "")
    assert not promotion_worktree_path(root, "fanatic/task").exists()


def test_dirty_repository_is_rejected_without_resources(tmp_path: Path) -> None:
    root = repository(tmp_path)
    result = implementation(root)
    (root / "untracked.txt").write_text("dirty", encoding="utf-8")
    promotion = promote(root, result)
    assert promotion.status == "repository_dirty"
    assert not promotion_worktree_path(root, "fanatic/task").exists()
    assert git(root, "show-ref", "--verify", "refs/heads/fanatic/task", check=False).returncode == 1


def test_changed_head_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    result = implementation(root)
    (root / "modify.txt").write_text("new base\n", encoding="utf-8")
    git(root, "add", "modify.txt")
    git(root, "commit", "-m", "head moved")
    assert promote(root, result).status == "base_changed"


@pytest.mark.parametrize(
    ("branch", "allowed"),
    [
        ("fanatic/fix-add", True),
        ("fanatic/task-user-validation", True),
        ("main", False),
        ("master", False),
        ("develop", False),
        ("fanatic/main", False),
        ("feature/task", False),
        ("fanatic/", False),
    ],
)
def test_branch_name_policy(branch: str, allowed: bool) -> None:
    assert branch_policy_allows(branch) is allowed


def test_invalid_git_ref_and_existing_branch_are_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    result = implementation(root)
    assert promote(root, result, "fanatic/bad..ref").status == "branch_rejected"
    git(root, "branch", "fanatic/existing")
    assert promote(root, result, "fanatic/existing").status == "branch_exists"


def test_worktree_location_is_outside_original_repository(tmp_path: Path) -> None:
    root = repository(tmp_path)
    destination = promotion_worktree_path(root, "fanatic/task")
    with pytest.raises(ValueError):
        destination.relative_to(root)
    assert destination.parent.name == root.name


def test_change_policy_is_revalidated_before_creation(tmp_path: Path) -> None:
    root = repository(tmp_path)

    class RejectingPolicy:
        calls = 0

        def validate(self, *_args, **_kwargs) -> ChangePolicyResult:
            self.calls += 1
            return ChangePolicyResult(
                status="rejected",
                issues=[
                    ChangePolicyIssue(
                        path="modify.txt", status="rejected", reason="test rejection"
                    )
                ],
            )

    policy = RejectingPolicy()
    promotion = VerifiedChangePromotionService(change_policy=policy).promote(
        repository=root,
        implementation=implementation(root),
        branch="fanatic/task",
        files_likely_affected=["modify.txt", "created.txt", "delete.txt"],
    )
    assert promotion.status == "policy_rejected" and policy.calls == 1
    assert not promotion_worktree_path(root, "fanatic/task").exists()
    assert git(root, "show-ref", "--verify", "refs/heads/fanatic/task", check=False).returncode == 1


def test_success_applies_exact_changes_and_protects_source(tmp_path: Path) -> None:
    root = repository(tmp_path)
    source_branch = git(root, "branch", "--show-current").stdout.strip()
    source_head = git(root, "rev-parse", "HEAD").stdout.strip()
    commit_count = git(root, "rev-list", "--count", "HEAD").stdout.strip()

    promotion = promote(root, implementation(root))

    assert promotion.status == "promoted" and promotion.worktree_path is not None
    worktree = Path(promotion.worktree_path)
    assert worktree.exists()
    assert (worktree / "modify.txt").read_text(encoding="utf-8") == "verified modification\n"
    assert (worktree / "created.txt").read_text(encoding="utf-8") == "verified creation\n"
    assert not (worktree / "delete.txt").exists()
    changed = git(worktree, "status", "--porcelain", "--untracked-files=all").stdout
    assert {line[3:] for line in changed.splitlines()} == {
        "modify.txt",
        "created.txt",
        "delete.txt",
    }
    assert (root / "modify.txt").read_text(encoding="utf-8") == "original\n"
    assert (root / "delete.txt").read_text(encoding="utf-8") == "delete me\n"
    assert not (root / "created.txt").exists()
    assert git(root, "status", "--porcelain").stdout == ""
    assert git(root, "branch", "--show-current").stdout.strip() == source_branch
    assert git(root, "rev-parse", "HEAD").stdout.strip() == source_head
    assert git(worktree, "rev-parse", "HEAD").stdout.strip() == source_head
    assert git(root, "rev-parse", "refs/heads/fanatic/task").stdout.strip() == source_head
    assert git(root, "rev-list", "--count", "fanatic/task").stdout.strip() == commit_count


@pytest.mark.parametrize("failure", ["unexpected_path", "wrong_content"])
def test_post_apply_failure_rolls_back_owned_resources(
    tmp_path: Path, failure: str
) -> None:
    root = repository(tmp_path)
    git(root, "branch", "unrelated")

    class InvalidApplier:
        def apply(self, selected: ChangeSet, workspace: Path):
            applied = ChangeSetApplier().apply(selected, workspace)
            if failure == "unexpected_path":
                (workspace / "unexpected.txt").write_text("unexpected", encoding="utf-8")
            else:
                (workspace / "modify.txt").write_text("wrong", encoding="utf-8")
            return applied

    result = VerifiedChangePromotionService(applier=InvalidApplier()).promote(
        repository=root,
        implementation=implementation(root),
        branch="fanatic/failing",
        files_likely_affected=["modify.txt", "created.txt", "delete.txt"],
    )
    assert result.status == "promotion_failed"
    assert not promotion_worktree_path(root, "fanatic/failing").exists()
    assert git(root, "show-ref", "--verify", "refs/heads/fanatic/failing", check=False).returncode == 1
    assert git(root, "show-ref", "--verify", "refs/heads/unrelated").returncode == 0


def test_git_subprocesses_disable_shell_and_promotion_never_commits_or_pushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = repository(tmp_path)
    original_run = subprocess.run
    calls: list[tuple[list[str], dict[str, object]]] = []

    def recording_run(argv, **kwargs):
        calls.append((list(argv), kwargs.copy()))
        return original_run(argv, **kwargs)

    monkeypatch.setattr("fanatic_agents.git.worktree.subprocess.run", recording_run)
    promotion = promote(root, implementation(root), "fanatic/no-remote")
    assert promotion.status == "promoted"
    assert calls and all(kwargs.get("shell") is False for _, kwargs in calls)
    subcommands = [argv[1] for argv, _ in calls if len(argv) > 1]
    assert not {"add", "commit", "push", "merge"}.intersection(subcommands)
