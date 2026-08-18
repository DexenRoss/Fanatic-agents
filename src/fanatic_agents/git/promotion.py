"""Deterministic promotion of an already-verified ChangeSet."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from fanatic_agents.delivery.models import (
    ExpectedPromotedChange,
    PromotionReceipt,
    VerificationSummary,
)
from fanatic_agents.delivery.receipt import (
    PromotionReceiptStore,
    ReceiptError,
    repository_identifier,
)
from fanatic_agents.git.errors import GitPromotionError, RepositoryStateError
from fanatic_agents.git.models import BaseRepositoryState, PromotionResult
from fanatic_agents.git.worktree import PromotionWorktree, RepositoryStateReader
from fanatic_agents.implementation.apply import ChangeSetApplier
from fanatic_agents.implementation.errors import ChangeApplicationError
from fanatic_agents.implementation.models import (
    ChangeSet,
    ImplementationResult,
    changeset_sha256,
)
from fanatic_agents.implementation.policy import ChangePolicy

PROTECTED_BRANCH_NAMES = frozenset(
    {"main", "master", "develop", "trunk", "production", "release", "head"}
)
SAFE_PATH_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def capture_base_repository_state(repository: Path) -> BaseRepositoryState:
    """Record the Git base used later to prevent promotion drift."""
    try:
        return RepositoryStateReader().capture(repository)
    except RepositoryStateError:
        raise
    except GitPromotionError as exc:
        raise RepositoryStateError(
            "repository_invalid", "Git repository state could not be captured safely."
        ) from exc


def promotion_worktree_path(repository: Path, branch: str) -> Path:
    """Return a deterministic sibling path that cannot traverse user input."""
    root = Path(repository).resolve(strict=True)
    container = root.parent / ".fanatic-agents-worktrees"
    project_container = container / root.name
    for candidate in (container, project_container):
        if candidate.is_symlink() or (candidate.exists() and not candidate.is_dir()):
            raise ValueError("Promotion worktree containers must be real directories.")
    readable = SAFE_PATH_COMPONENT.sub("-", branch).strip(".-_") or "promotion"
    suffix = hashlib.sha256(branch.encode("utf-8")).hexdigest()[:10]
    destination = (project_container / f"{readable}-{suffix}").resolve(strict=False)
    try:
        destination.relative_to(root)
    except ValueError:
        return destination
    raise ValueError("Promotion worktrees must be outside the original repository.")


def branch_policy_allows(branch: str) -> bool:
    """Apply Fanatic Agents' deny-by-default local branch policy."""
    lowered = branch.casefold()
    if lowered in PROTECTED_BRANCH_NAMES or not branch.startswith("fanatic/"):
        return False
    suffix = branch.removeprefix("fanatic/")
    return bool(suffix) and suffix.casefold() not in PROTECTED_BRANCH_NAMES


class VerifiedChangePromotionService:
    """Promote one verified result without model calls, commits, or pushes."""

    def __init__(
        self,
        *,
        state_reader: RepositoryStateReader | None = None,
        change_policy: ChangePolicy | None = None,
        applier: ChangeSetApplier | None = None,
        receipt_store: PromotionReceiptStore | None = None,
    ) -> None:
        self._state_reader = state_reader or RepositoryStateReader()
        self._change_policy = change_policy or ChangePolicy()
        self._applier = applier or ChangeSetApplier()
        self._receipt_store = receipt_store or PromotionReceiptStore()

    def promote(
        self,
        *,
        repository: Path,
        implementation: ImplementationResult,
        branch: str,
        files_likely_affected: list[str],
    ) -> PromotionResult:
        root_text = str(Path(repository).expanduser().resolve(strict=False))
        base = implementation.base_repository_state
        common = {
            "repository": root_text,
            "base_branch": base.branch if base else None,
            "base_commit": base.commit_sha if base else None,
            "promoted_branch": branch,
            "changes": len(implementation.changeset.changes)
            if implementation.changeset is not None
            else 0,
        }
        if implementation.status != "verified":
            return PromotionResult(
                **common,
                status="not_verified",
                stop_reason="Only a VERIFIED ImplementationResult can be promoted.",
            )
        if implementation.changeset is None or base is None:
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="Verified promotion requires its recorded ChangeSet and base state.",
            )
        if implementation.verified_changeset_sha256 != changeset_sha256(
            implementation.changeset
        ):
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="The ChangeSet no longer matches the version that was verified.",
            )

        try:
            current = self._state_reader.capture(repository)
        except RepositoryStateError as exc:
            return PromotionResult(**common, status=exc.status, stop_reason=str(exc))
        except GitPromotionError:
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="Git repository state could not be captured safely.",
            )
        if not current.working_tree_clean:
            return PromotionResult(
                **common,
                status="repository_dirty",
                stop_reason="Promotion requires the original working tree to be clean.",
            )
        if (
            current.repository_path != base.repository_path
            or current.branch != base.branch
            or current.commit_sha != base.commit_sha
        ):
            return PromotionResult(
                **common,
                status="base_changed",
                stop_reason="Repository branch or HEAD changed after verification.",
            )
        if not base.working_tree_clean:
            return PromotionResult(
                **common,
                status="repository_dirty",
                stop_reason="Promotion requires a clean recorded base repository.",
            )

        try:
            worktree = PromotionWorktree(Path(current.repository_path))
            valid_branch = worktree.validate_branch(branch)
            exists = worktree.branch_exists(branch) if valid_branch else False
        except (GitPromotionError, OSError):
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="Local Git branch state could not be inspected safely.",
            )
        if not branch_policy_allows(branch) or not valid_branch:
            return PromotionResult(
                **common,
                status="branch_rejected",
                stop_reason="Promotion requires a valid new branch under fanatic/.",
            )
        if exists:
            return PromotionResult(
                **common,
                status="branch_exists",
                stop_reason="The requested local branch already exists and will not be reused.",
            )

        try:
            policy = self._change_policy.validate(
                implementation.changeset,
                workspace=Path(current.repository_path),
                files_likely_affected=files_likely_affected,
            )
        except (OSError, ValueError):
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="ChangePolicy could not safely revalidate the ChangeSet.",
            )
        if policy.status != "approved":
            return PromotionResult(
                **common,
                status="policy_rejected",
                stop_reason="ChangePolicy rejected the ChangeSet during promotion revalidation.",
            )

        try:
            destination = promotion_worktree_path(Path(current.repository_path), branch)
        except (OSError, ValueError):
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="A safe promotion worktree path could not be selected.",
            )
        if destination.exists() or destination.is_symlink():
            return PromotionResult(
                **common,
                status="promotion_failed",
                stop_reason="The dedicated promotion worktree path already exists.",
            )

        creation_started = False
        try:
            creation_started = True
            worktree.create(branch, destination, base.commit_sha)
            self._applier.apply(implementation.changeset, destination)
            self._validate_applied_changes(
                implementation.changeset, destination, worktree, base.commit_sha, branch
            )
            final_source = self._state_reader.capture(repository)
            if (
                not final_source.working_tree_clean
                or final_source.branch != base.branch
                or final_source.commit_sha != base.commit_sha
            ):
                raise GitPromotionError("The original repository changed during promotion.")
            now = datetime.now(UTC)
            receipt = PromotionReceipt(
                repository_id=repository_identifier(Path(current.repository_path)),
                repository_path=current.repository_path,
                base_branch=base.branch,
                base_commit=base.commit_sha,
                promoted_branch=branch,
                worktree_path=str(destination),
                task_title=implementation.changeset.task_title,
                expected_changes=[
                    ExpectedPromotedChange(
                        path=change.path,
                        operation=change.operation,
                        content_sha256=(
                            hashlib.sha256((change.content or "").encode("utf-8")).hexdigest()
                            if change.operation != "delete"
                            else None
                        ),
                    )
                    for change in implementation.changeset.changes
                ],
                implementation_status="verified",
                promotion_status="promoted",
                verification_summary=[
                    VerificationSummary(
                        argv=result.argv,
                        exit_code=result.exit_code,
                        timed_out=result.timed_out,
                        passed=result.exit_code == 0 and not result.timed_out,
                    )
                    for result in implementation.verification_results
                ],
                created_at=now,
                updated_at=now,
            )
            self._receipt_store.save(receipt)
        except (
            ChangeApplicationError,
            GitPromotionError,
            OSError,
            ReceiptError,
            UnicodeError,
        ):
            rollback_confirmed = True
            if creation_started:
                try:
                    worktree.rollback_failed_promotion(branch, destination)
                except GitPromotionError:
                    rollback_confirmed = False
            return PromotionResult(
                **common,
                worktree_path=(
                    str(destination)
                    if not rollback_confirmed and destination.exists()
                    else None
                ),
                status="promotion_failed",
                stop_reason=(
                    "Promotion failed safely and its created resources were rolled back."
                    if rollback_confirmed
                    else "Promotion failed and automatic rollback could not be confirmed; "
                    "manual inspection is required."
                ),
            )

        return PromotionResult(
            **common,
            worktree_path=str(destination),
            status="promoted",
        )

    @staticmethod
    def _validate_applied_changes(
        changeset: ChangeSet,
        destination: Path,
        worktree: PromotionWorktree,
        base_commit: str,
        branch: str,
    ) -> None:
        expected_paths = {change.path for change in changeset.changes}
        for change in changeset.changes:
            target = destination.joinpath(*PurePosixPath(change.path).parts)
            if change.operation == "delete":
                if target.exists() or target.is_symlink():
                    raise GitPromotionError("A promoted delete did not match the ChangeSet.")
                continue
            if not target.is_file() or target.is_symlink():
                raise GitPromotionError("A promoted file did not match the ChangeSet.")
            if target.read_text(encoding="utf-8") != change.content:
                raise GitPromotionError("Promoted content did not exactly match the ChangeSet.")
        if worktree.changed_paths(destination) != expected_paths:
            raise GitPromotionError("Unexpected paths changed in the promotion worktree.")
        if worktree.commit(destination) != base_commit:
            raise GitPromotionError("The promotion worktree no longer points at the exact base.")
        if worktree.branch_commit(branch) != base_commit:
            raise GitPromotionError("The promoted branch no longer points at the exact base.")


def promote_verified_changes(
    repository: Path,
    implementation: ImplementationResult,
    branch: str,
    files_likely_affected: list[str],
) -> PromotionResult:
    """CLI boundary for one deterministic verified-change promotion."""
    return VerifiedChangePromotionService().promote(
        repository=repository,
        implementation=implementation,
        branch=branch,
        files_likely_affected=files_likely_affected,
    )
