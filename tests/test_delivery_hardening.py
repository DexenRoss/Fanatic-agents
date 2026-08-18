"""Additional fail-closed delivery provenance and locking tests."""

from __future__ import annotations

import os
from pathlib import Path

from fanatic_agents.delivery.models import PromotionReceipt
from fanatic_agents.delivery.receipt import (
    PromotionReceiptStore,
    receipt_path_for_worktree,
    repository_identifier,
)
from fanatic_agents.delivery.service import DeliveryService
from test_delivery import FakeGitHub, NetworklessGit, promote, repository, service


def rewrite_receipt(worktree: Path, **updates: object) -> None:
    store = PromotionReceiptStore()
    values = store.load(worktree).model_dump()
    values.update(updates)
    store.save(PromotionReceipt.model_validate(values))


def test_receipt_pointing_at_another_repository_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path / "source")
    other = repository(tmp_path / "other")
    worktree, _ = promote(root)
    rewrite_receipt(
        worktree,
        repository_path=str(other.resolve()),
        repository_id=repository_identifier(other),
    )
    assert service().deliver(worktree).status == "invalid_promotion"


def test_non_fanatic_receipt_branch_is_rejected(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, _ = promote(root)
    rewrite_receipt(worktree, promoted_branch="main")
    assert service().deliver(worktree).status == "invalid_promotion"


def test_configuration_for_another_repository_denies_delivery(tmp_path: Path) -> None:
    root = repository(tmp_path / "source")
    other = repository(tmp_path / "other")
    worktree, receipt = promote(root)
    result = service().deliver(worktree, configured_repository=other)
    assert result.status == "permission_denied"
    assert NetworklessGit().run(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit


def test_active_local_lock_fails_closed_without_effects(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    lock = receipt_path_for_worktree(worktree).with_suffix(".lock")
    lock.write_text(f"pid={os.getpid()}\n", encoding="utf-8")
    result = service().deliver(worktree)
    assert result.status == "delivery_in_progress"
    assert NetworklessGit().run(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit


def test_non_github_origin_is_rejected_before_commit(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    NetworklessGit().run(root, "remote", "set-url", "origin", "https://example.invalid/repo.git")
    result = service().deliver(worktree)
    assert result.status == "delivery_failed"
    assert NetworklessGit().run(worktree, "rev-parse", "HEAD").stdout.strip() == receipt.base_commit


def test_pr_body_redacts_sensitive_assignments(tmp_path: Path) -> None:
    root = repository(tmp_path)
    worktree, receipt = promote(root)
    rewrite_receipt(worktree, task_title="Rotate token=synthetic-secret-value")
    github = FakeGitHub()
    result = DeliveryService(git=NetworklessGit(), github=github).deliver(worktree)
    assert result.status == "delivered"
    assert "synthetic-secret-value" not in github.created["body"]
    assert "[REDACTED]" in github.created["body"]
    assert result.commit_sha
