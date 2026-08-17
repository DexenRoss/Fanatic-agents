"""Deterministic, review-only delivery state machine."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Protocol

from fanatic_agents.core.config import PermissionsConfig
from fanatic_agents.delivery.models import DeliveryResult, PromotionReceipt
from fanatic_agents.delivery.receipt import (
    DeliveryLockedError,
    PromotionReceiptStore,
    ReceiptError,
    repository_identifier,
)
from fanatic_agents.git.errors import GitCommandError
from fanatic_agents.git.promotion import branch_policy_allows
from fanatic_agents.git.worktree import GitRunner
from fanatic_agents.github.client import (
    GitHubCli,
    GitHubCommandError,
    GitHubPreflight,
    PullRequestReference,
    parse_github_repository,
)

MAX_SUBJECT_LENGTH = 200
CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f]")
SENSITIVE_TEXT = re.compile(
    r"(?i)(?:api[_-]?key|password|secret|token)(?:\s*[:=]\s*|\s+)[^\s]+"
)


class GitHubDeliveryClient(Protocol):
    def preflight(self) -> GitHubPreflight: ...

    def find_pull_request(
        self, repository: str, *, base: str, head: str
    ) -> PullRequestReference | None: ...

    def create_pull_request(
        self,
        repository: str,
        *,
        base: str,
        head: str,
        title: str,
        body: str,
    ) -> PullRequestReference: ...


class DeliveryValidationError(RuntimeError):
    """Local provenance or exact-change validation failed closed."""

    def __init__(self, status: str, message: str) -> None:
        super().__init__(message)
        self.status = status


class DeliveryService:
    """Advance one promotion through commit, push, and pull request exactly once."""

    def __init__(
        self,
        *,
        git: GitRunner | None = None,
        github: GitHubDeliveryClient | None = None,
        receipts: PromotionReceiptStore | None = None,
    ) -> None:
        self._git = git or GitRunner(timeout_seconds=20.0)
        self._github = github or GitHubCli()
        self._receipts = receipts or PromotionReceiptStore()

    def deliver(
        self,
        worktree: Path,
        *,
        permissions: PermissionsConfig | None = None,
        configured_repository: Path | None = None,
        commit_message: str | None = None,
        pr_title: str | None = None,
        check_only: bool = False,
    ) -> DeliveryResult:
        requested = Path(worktree).expanduser().resolve(strict=False)
        fallback = DeliveryResult(
            repository=str(requested),
            worktree_path=str(requested),
            status="invalid_promotion",
        )
        try:
            validated_message = _validate_subject(commit_message, "commit message")
            validated_title = _validate_subject(pr_title, "pull request title")
        except ValueError as exc:
            return fallback.model_copy(
                update={"status": "delivery_failed", "stop_reason": str(exc)}
            )
        try:
            with self._receipts.lock(requested):
                return self._deliver_locked(
                    requested,
                    permissions=permissions,
                    configured_repository=configured_repository,
                    commit_message=validated_message,
                    pr_title=validated_title,
                    check_only=check_only,
                )
        except DeliveryLockedError as exc:
            return fallback.model_copy(
                update={"status": "delivery_in_progress", "stop_reason": str(exc)}
            )
        except ReceiptError as exc:
            return fallback.model_copy(update={"stop_reason": str(exc)})

    def _deliver_locked(
        self,
        worktree: Path,
        *,
        permissions: PermissionsConfig | None,
        configured_repository: Path | None,
        commit_message: str | None,
        pr_title: str | None,
        check_only: bool,
    ) -> DeliveryResult:
        try:
            receipt = self._receipts.load(worktree)
            self._validate_receipt(receipt, worktree)
            if configured_repository is not None and (
                configured_repository.expanduser().resolve(strict=True)
                != Path(receipt.repository_path).expanduser().resolve(strict=True)
            ):
                raise DeliveryValidationError(
                    "permission_denied", "Project configuration belongs to another repository."
                )
            receipt = self._validate_git_state(receipt, worktree)
        except ReceiptError as exc:
            return _result_for_path(worktree, "invalid_promotion", str(exc))
        except DeliveryValidationError as exc:
            return _result_for_path(worktree, exc.status, str(exc))
        except (GitCommandError, OSError):
            return _result_for_path(
                worktree,
                "invalid_promotion",
                "Promotion Git state could not be validated safely.",
            )

        result = _result(receipt, "delivery_failed")
        denied = self._permission_denial(receipt, permissions)
        if denied is not None:
            return _result(receipt, "permission_denied", denied)

        remote_result = self._git.run(worktree, "remote", "get-url", "origin")
        if remote_result.returncode != 0 or not remote_result.stdout.strip():
            return _result(receipt, "delivery_failed", "Delivery requires the origin remote.")
        github_repository = parse_github_repository(remote_result.stdout.strip())
        if github_repository is None:
            return _result(
                receipt,
                "delivery_failed",
                "The origin remote must be a supported GitHub HTTPS or SSH URL.",
            )
        try:
            preflight = self._github.preflight()
        except GitHubCommandError:
            return _result(
                receipt, "github_auth_required", "GitHub CLI authentication could not be verified."
            )
        if preflight.status == "not_found":
            return _result(
                receipt,
                "github_cli_unavailable",
                "GitHub CLI is required for delivery.",
            )
        if preflight.status != "ok":
            return _result(
                receipt,
                "github_auth_required",
                "GitHub CLI is installed but not authenticated; run gh auth login.",
            )

        if check_only:
            if receipt.delivery_stage == "promoted":
                remote_sha = self._remote_branch_sha(worktree, receipt.promoted_branch)
                if remote_sha is None:
                    return _result(
                        receipt,
                        "delivery_failed",
                        "The remote branch state could not be inspected safely.",
                    )
                if remote_sha:
                    return _result(
                        receipt,
                        "remote_branch_exists",
                        "The remote branch already exists and will not be overwritten.",
                    )
            return _result(receipt, "ready", "Delivery preflight passed; no changes were made.")

        subject = commit_message or _default_subject(receipt.task_title)
        title = pr_title or subject
        if receipt.delivery_stage == "promoted":
            staged = self._stage_and_commit(receipt, worktree, subject)
            if isinstance(staged, DeliveryResult):
                return staged
            receipt = staged

        if receipt.delivery_stage == "commit_created":
            remote_sha = self._remote_branch_sha(worktree, receipt.promoted_branch)
            if remote_sha is None:
                return _result(
                    receipt,
                    "push_failed",
                    "The commit was created, but remote branch state could not be inspected.",
                )
            if remote_sha:
                return _result(
                    receipt,
                    "remote_branch_exists",
                    "The commit was created, but the remote branch already exists; it was not overwritten.",
                )
            pushed = self._git.run(
                worktree,
                "push",
                "--set-upstream",
                "origin",
                receipt.promoted_branch,
            )
            if pushed.returncode != 0:
                return _result(
                    receipt,
                    "push_failed",
                    "The local commit was preserved, but push to origin failed.",
                )
            receipt = _updated_receipt(
                receipt,
                delivery_stage="branch_pushed",
                remote="origin",
                remote_branch=receipt.promoted_branch,
            )
            try:
                self._receipts.save(receipt)
            except ReceiptError:
                return _result(
                    receipt,
                    "delivery_failed",
                    "The branch was pushed, but local delivery state could not be persisted.",
                )

        if receipt.delivery_stage == "branch_pushed":
            body = _pull_request_body(receipt)
            try:
                pull_request = self._github.find_pull_request(
                    github_repository,
                    base=receipt.base_branch,
                    head=receipt.promoted_branch,
                )
                if pull_request is None:
                    pull_request = self._github.create_pull_request(
                        github_repository,
                        base=receipt.base_branch,
                        head=receipt.promoted_branch,
                        title=title,
                        body=body,
                    )
            except GitHubCommandError:
                return _result(
                    receipt,
                    "pr_creation_failed",
                    "The branch remains pushed, but pull request creation failed.",
                )
            receipt = _updated_receipt(
                receipt,
                delivery_stage="pr_created",
                pr_number=pull_request.number,
                pr_url=pull_request.url,
            )
            try:
                self._receipts.save(receipt)
            except ReceiptError:
                return _result(
                    receipt,
                    "delivery_failed",
                    "The pull request exists, but local delivery state could not be persisted.",
                )

        if receipt.delivery_stage == "pr_created":
            return _result(receipt, "delivered")
        return result

    def _validate_receipt(self, receipt: PromotionReceipt, worktree: Path) -> None:
        if not worktree.is_dir() or worktree.is_symlink():
            raise DeliveryValidationError(
                "invalid_promotion", "Delivery requires an existing real promotion worktree."
            )
        if Path(receipt.worktree_path).expanduser().resolve(strict=True) != worktree.resolve(
            strict=True
        ):
            raise DeliveryValidationError(
                "invalid_promotion", "The promotion receipt belongs to another worktree."
            )
        repository = Path(receipt.repository_path).expanduser()
        if not repository.is_dir() or repository.is_symlink():
            raise DeliveryValidationError(
                "invalid_promotion", "The recorded source repository is unavailable."
            )
        if repository_identifier(repository) != receipt.repository_id:
            raise DeliveryValidationError(
                "invalid_promotion", "The promotion receipt repository does not match."
            )
        if not branch_policy_allows(receipt.promoted_branch):
            raise DeliveryValidationError(
                "invalid_promotion", "Delivery permits only validated fanatic/* branches."
            )
        for expected in receipt.expected_changes:
            _safe_target(worktree, expected.path)

    def _validate_git_state(
        self, receipt: PromotionReceipt, worktree: Path
    ) -> PromotionReceipt:
        repository = Path(receipt.repository_path).resolve(strict=True)
        top = _required_git_text(self._git, worktree, "rev-parse", "--show-toplevel")
        if Path(top).resolve(strict=True) != worktree.resolve(strict=True):
            raise DeliveryValidationError(
                "invalid_promotion", "Delivery must start at the promotion worktree root."
            )
        common = _required_git_text(self._git, worktree, "rev-parse", "--git-common-dir")
        common_path = Path(common)
        if not common_path.is_absolute():
            common_path = worktree / common_path
        if common_path.resolve(strict=True) != (repository / ".git").resolve(strict=True):
            raise DeliveryValidationError(
                "invalid_promotion", "The worktree belongs to a different repository."
            )
        listing = self._git.run(repository, "worktree", "list", "--porcelain")
        if listing.returncode != 0 or not _worktree_is_registered(listing.stdout, worktree):
            raise DeliveryValidationError(
                "invalid_promotion", "The promotion worktree is not registered with Git."
            )
        branch = _required_git_text(
            self._git, worktree, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if branch != receipt.promoted_branch:
            raise DeliveryValidationError(
                "invalid_promotion", "The promotion worktree branch does not match its receipt."
            )
        head = _required_git_text(self._git, worktree, "rev-parse", "--verify", "HEAD")
        branch_head = _required_git_text(
            self._git,
            repository,
            "rev-parse",
            "--verify",
            f"refs/heads/{receipt.promoted_branch}",
        )
        if branch_head != head:
            raise DeliveryValidationError(
                "invalid_promotion", "The local promoted branch changed unexpectedly."
            )

        if receipt.delivery_stage == "promoted":
            if head != receipt.base_commit:
                recovered = self._recover_single_commit(receipt, worktree, head)
                if recovered is None:
                    raise DeliveryValidationError(
                        "invalid_promotion", "HEAD no longer points to the recorded base commit."
                    )
                receipt = recovered
                self._receipts.save(receipt)
            else:
                cached = self._git.run(worktree, "diff", "--cached", "--quiet")
                if cached.returncode != 0:
                    raise DeliveryValidationError(
                        "modified_after_verification",
                        "The promotion index was modified before delivery.",
                    )
                self._validate_uncommitted_changes(receipt, worktree)
        else:
            if receipt.commit_sha != head:
                raise DeliveryValidationError(
                    "invalid_promotion", "The recorded delivery commit no longer matches HEAD."
                )
            self._validate_committed_changes(receipt, worktree, head)
        return receipt

    def _recover_single_commit(
        self, receipt: PromotionReceipt, worktree: Path, head: str
    ) -> PromotionReceipt | None:
        try:
            self._validate_committed_changes(receipt, worktree, head)
        except DeliveryValidationError:
            return None
        return _updated_receipt(receipt, delivery_stage="commit_created", commit_sha=head)

    def _validate_uncommitted_changes(
        self, receipt: PromotionReceipt, worktree: Path
    ) -> None:
        self._validate_content(receipt, worktree)
        changed = _status_paths(self._git, worktree)
        expected = {change.path for change in receipt.expected_changes}
        if changed != expected:
            raise DeliveryValidationError(
                "modified_after_verification",
                "Worktree paths no longer exactly match the verified promotion.",
            )

    def _validate_committed_changes(
        self, receipt: PromotionReceipt, worktree: Path, head: str
    ) -> None:
        parent = _required_git_text(self._git, worktree, "rev-parse", f"{head}^")
        if parent != receipt.base_commit:
            raise DeliveryValidationError(
                "invalid_promotion", "The delivery commit parent is not the recorded base."
            )
        status = self._git.run(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
        if status.returncode != 0 or status.stdout:
            raise DeliveryValidationError(
                "modified_after_verification", "The committed delivery worktree is not clean."
            )
        self._validate_content(receipt, worktree)
        staged = _name_status(
            self._git, worktree, "diff", "--name-status", "--no-renames", "-z",
            receipt.base_commit, head,
        )
        if staged != _expected_statuses(receipt):
            raise DeliveryValidationError(
                "modified_after_verification", "The delivery commit is not the exact verified change."
            )

    @staticmethod
    def _validate_content(receipt: PromotionReceipt, worktree: Path) -> None:
        for expected in receipt.expected_changes:
            target = _safe_target(worktree, expected.path)
            if expected.operation == "delete":
                if target.exists() or target.is_symlink():
                    raise DeliveryValidationError(
                        "modified_after_verification", "A verified deleted path was restored."
                    )
                continue
            if not target.is_file() or target.is_symlink():
                raise DeliveryValidationError(
                    "modified_after_verification", "A verified promoted file is missing or unsafe."
                )
            try:
                digest = hashlib.sha256(target.read_bytes()).hexdigest()
            except OSError as exc:
                raise DeliveryValidationError(
                    "modified_after_verification", "Promoted content could not be inspected."
                ) from exc
            if digest != expected.content_sha256:
                raise DeliveryValidationError(
                    "modified_after_verification", "Promoted content changed after verification."
                )

    def _stage_and_commit(
        self, receipt: PromotionReceipt, worktree: Path, subject: str
    ) -> PromotionReceipt | DeliveryResult:
        paths = [change.path for change in receipt.expected_changes]
        staged = self._git.run(worktree, "add", "--all", "--", *paths)
        if staged.returncode != 0:
            self._unstage(worktree, paths)
            return _result(receipt, "staging_failed", "Exact-path staging failed; no commit was created.")
        try:
            observed = _name_status(
                self._git, worktree, "diff", "--cached", "--name-status", "--no-renames", "-z"
            )
        except GitCommandError:
            self._unstage(worktree, paths)
            return _result(receipt, "staging_failed", "The staged change set could not be verified.")
        if observed != _expected_statuses(receipt):
            self._unstage(worktree, paths)
            return _result(
                receipt, "staging_failed", "The staged paths were not the exact verified ChangeSet."
            )
        committed = self._git.run(worktree, "commit", "-m", subject)
        if committed.returncode != 0:
            self._unstage(worktree, paths)
            return _result(
                receipt,
                "commit_failed",
                "Commit failed or was rejected by a Git hook; push and PR were not attempted.",
            )
        commit_sha = _optional_git_text(self._git, worktree, "rev-parse", "--verify", "HEAD")
        parent = _optional_git_text(self._git, worktree, "rev-parse", "HEAD^")
        branch = _optional_git_text(
            self._git, worktree, "symbolic-ref", "--quiet", "--short", "HEAD"
        )
        if not commit_sha or parent != receipt.base_commit or branch != receipt.promoted_branch:
            return _result(
                receipt,
                "commit_failed",
                "A commit was created, but its parent or branch could not be validated.",
            )
        updated = _updated_receipt(
            receipt, delivery_stage="commit_created", commit_sha=commit_sha
        )
        try:
            self._receipts.save(updated)
        except ReceiptError:
            return _result(
                updated,
                "delivery_failed",
                "The commit was created, but local delivery state could not be persisted.",
            )
        return updated

    def _remote_branch_sha(self, worktree: Path, branch: str) -> str | None:
        result = self._git.run(
            worktree, "ls-remote", "--heads", "origin", f"refs/heads/{branch}"
        )
        if result.returncode != 0:
            return None
        output = result.stdout.strip()
        if not output:
            return ""
        first = output.splitlines()[0].split()
        return first[0] if len(first) == 2 else None

    def _unstage(self, worktree: Path, paths: list[str]) -> None:
        self._git.run(worktree, "restore", "--staged", "--", *paths)

    @staticmethod
    def _permission_denial(
        receipt: PromotionReceipt, permissions: PermissionsConfig | None
    ) -> str | None:
        if permissions is None:
            return None
        if receipt.delivery_stage == "promoted" and not permissions.commit:
            return "Project configuration denies the commit capability."
        if receipt.delivery_stage in {"promoted", "commit_created"} and not permissions.push_branch:
            return "Project configuration denies the push_branch capability."
        if receipt.delivery_stage != "pr_created" and not permissions.create_pull_request:
            return "Project configuration denies the create_pull_request capability."
        return None


def deliver_promotion(
    worktree: Path,
    *,
    permissions: PermissionsConfig | None = None,
    configured_repository: Path | None = None,
    commit_message: str | None = None,
    pr_title: str | None = None,
    check_only: bool = False,
) -> DeliveryResult:
    """CLI boundary for deterministic GitHub delivery with zero model calls."""
    return DeliveryService().deliver(
        worktree,
        permissions=permissions,
        configured_repository=configured_repository,
        commit_message=commit_message,
        pr_title=pr_title,
        check_only=check_only,
    )


def _safe_target(worktree: Path, value: str) -> Path:
    if "\x00" in value or "\\" in value or value.startswith("/"):
        raise DeliveryValidationError("invalid_promotion", "Receipt contains an unsafe path.")
    relative = PurePosixPath(value)
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise DeliveryValidationError("invalid_promotion", "Receipt contains an unsafe path.")
    target = worktree.joinpath(*relative.parts)
    try:
        target.resolve(strict=False).relative_to(worktree.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise DeliveryValidationError("invalid_promotion", "Receipt path escaped the worktree.") from exc
    return target


def _required_git_text(git: GitRunner, repository: Path, *arguments: str) -> str:
    value = _optional_git_text(git, repository, *arguments)
    if not value:
        raise DeliveryValidationError("invalid_promotion", "Required Git state is unavailable.")
    return value


def _optional_git_text(git: GitRunner, repository: Path, *arguments: str) -> str | None:
    result = git.run(repository, *arguments)
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _worktree_is_registered(output: str, worktree: Path) -> bool:
    expected = worktree.resolve(strict=True)
    for line in output.splitlines():
        if not line.startswith("worktree "):
            continue
        try:
            if Path(line.removeprefix("worktree ")).resolve(strict=True) == expected:
                return True
        except OSError:
            continue
    return False


def _status_paths(git: GitRunner, worktree: Path) -> set[str]:
    result = git.run(worktree, "status", "--porcelain", "-z", "--untracked-files=all")
    if result.returncode != 0:
        raise GitCommandError("Worktree status could not be inspected safely.")
    paths: set[str] = set()
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if not record:
            continue
        if len(record) < 4:
            raise GitCommandError("Git returned an unexpected worktree status.")
        path = record[3:]
        if record[:2] in {"R ", "C ", " R", " C"}:
            if index >= len(records) or not records[index]:
                raise GitCommandError("Git returned an unexpected worktree status.")
            path = records[index]
            index += 1
        paths.add(path)
    return paths


def _name_status(
    git: GitRunner, worktree: Path, *arguments: str
) -> dict[str, str]:
    result = git.run(worktree, *arguments)
    if result.returncode != 0:
        raise GitCommandError("Git change names could not be inspected safely.")
    fields = [field for field in result.stdout.split("\0") if field]
    if len(fields) % 2:
        raise GitCommandError("Git returned an unexpected name-status result.")
    observed: dict[str, str] = {}
    for index in range(0, len(fields), 2):
        status, path = fields[index], fields[index + 1]
        if status not in {"A", "M", "D"} or path in observed:
            raise GitCommandError("Git returned an unexpected name-status result.")
        observed[path] = status
    return observed


def _expected_statuses(receipt: PromotionReceipt) -> dict[str, str]:
    statuses = {"create": "A", "modify": "M", "delete": "D"}
    return {change.path: statuses[change.operation] for change in receipt.expected_changes}


def _updated_receipt(receipt: PromotionReceipt, **updates: object) -> PromotionReceipt:
    values = receipt.model_dump()
    values.update(updates)
    values["updated_at"] = datetime.now(UTC)
    return PromotionReceipt.model_validate(values)


def _result(
    receipt: PromotionReceipt, status: str, stop_reason: str | None = None
) -> DeliveryResult:
    return DeliveryResult(
        repository=receipt.repository_path,
        worktree_path=receipt.worktree_path,
        base_branch=receipt.base_branch,
        base_commit=receipt.base_commit,
        branch=receipt.promoted_branch,
        commit_sha=receipt.commit_sha,
        remote=receipt.remote,
        remote_branch=receipt.remote_branch,
        pr_number=receipt.pr_number,
        pr_url=receipt.pr_url,
        status=status,
        stop_reason=stop_reason,
    )


def _result_for_path(worktree: Path, status: str, reason: str) -> DeliveryResult:
    return DeliveryResult(
        repository=str(worktree), worktree_path=str(worktree), status=status, stop_reason=reason
    )


def _validate_subject(value: str | None, label: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"The {label} cannot be empty.")
    if len(normalized) > MAX_SUBJECT_LENGTH:
        raise ValueError(f"The {label} cannot exceed {MAX_SUBJECT_LENGTH} characters.")
    if CONTROL_CHARACTERS.search(normalized):
        raise ValueError(f"The {label} must be one line without control characters.")
    return normalized


def _default_subject(task_title: str) -> str:
    normalized = " ".join(task_title.split())
    prefix = "fanatic: "
    return f"{prefix}{normalized[: MAX_SUBJECT_LENGTH - len(prefix)]}"


def _pull_request_body(receipt: PromotionReceipt) -> str:
    changed = "\n".join(f"- `{change.path}` ({change.operation})" for change in receipt.expected_changes)
    if receipt.verification_summary:
        verification = "\n".join(
            f"- `{_redact(' '.join(item.argv))}`: {'PASS' if item.passed else 'FAIL'}"
            for item in receipt.verification_summary
        )
    else:
        verification = "- VERIFIED result recorded; no command summary was retained"
    task = _redact(receipt.task_title)
    return (
        "## Fanatic Agents Delivery\n\n"
        f"Task:\n{task}\n\n"
        "Implementation:\nVERIFIED\n\n"
        "Promotion:\nPROMOTED\n\n"
        f"Changed files:\n{changed}\n\n"
        f"Verification performed:\n{verification}\n\n"
        f"Base commit:\n`{receipt.base_commit[:12]}`\n\n"
        "Safety:\n"
        "- original working tree remained untouched\n"
        "- no automatic merge was performed\n\n"
        "Generated by Fanatic Agents."
    )


def _redact(value: str) -> str:
    return SENSITIVE_TEXT.sub("[REDACTED]", value)
