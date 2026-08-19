"""Atomic external storage for local task reservations."""

from __future__ import annotations

import hashlib
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from fanatic_agents.intake.models import TaskIntakeReceipt

METADATA_CONTAINER = ".fanatic-agents-worktrees"
ACTIVE_TASK_STATUSES = {
    "selected",
    "running",
    "promoted",
    "delivered",
    "waiting_for_review",
}


class TaskIntakeReceiptError(RuntimeError):
    """Task intake metadata could not be read or stored safely."""


class TaskIntakeLockedError(TaskIntakeReceiptError):
    """Another local intake operation currently owns the repository lock."""


class DuplicateTaskReceiptError(TaskIntakeReceiptError):
    """An active reservation already exists for the Issue."""


def intake_metadata_root(repository: Path) -> Path:
    """Return an external metadata root beside, never inside, the repository."""

    resolved = Path(repository).expanduser().resolve(strict=True)
    return (
        resolved.parent
        / METADATA_CONTAINER
        / ".metadata"
        / "intake"
        / hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    )


class TaskIntakeReceiptStore:
    """Persist strict Issue reservations without creating project files."""

    def __init__(self, *, metadata_root: Path | None = None) -> None:
        self._metadata_root = metadata_root

    def directory(self, repository: Path) -> Path:
        resolved = Path(repository).expanduser().resolve(strict=True)
        path = (
            Path(self._metadata_root).expanduser().resolve(strict=False)
            / hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
            if self._metadata_root is not None
            else intake_metadata_root(resolved)
        )
        try:
            path.resolve(strict=False).relative_to(resolved)
        except ValueError:
            return path
        raise TaskIntakeReceiptError("Task metadata must remain outside the repository.")

    def path_for(self, repository: Path, issue_number: int) -> Path:
        if issue_number <= 0:
            raise ValueError("issue_number must be greater than zero")
        return self.directory(repository) / f"issue-{issue_number}.json"

    def active_issue_numbers(
        self, repository: Path, github_repository: str
    ) -> set[int]:
        directory = self.directory(repository)
        if not directory.exists():
            return set()
        if directory.is_symlink() or not directory.is_dir():
            raise TaskIntakeReceiptError("Task metadata must be a real directory.")
        active: set[int] = set()
        try:
            paths = sorted(directory.glob("issue-*.json"))
            for path in paths:
                if path.is_symlink() or not path.is_file():
                    raise TaskIntakeReceiptError("Task intake metadata is invalid.")
                receipt = TaskIntakeReceipt.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if (
                    Path(receipt.repository).expanduser().resolve(strict=True)
                    != Path(repository).expanduser().resolve(strict=True)
                    or receipt.github_repository.casefold()
                    != github_repository.casefold()
                ):
                    raise TaskIntakeReceiptError("Task intake metadata is invalid.")
                if receipt.task_status in ACTIVE_TASK_STATUSES:
                    active.add(receipt.issue_number)
        except TaskIntakeReceiptError:
            raise
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise TaskIntakeReceiptError("Task intake metadata is invalid.") from exc
        return active

    def save(self, receipt: TaskIntakeReceipt) -> Path:
        repository = Path(receipt.repository)
        path = self.path_for(repository, receipt.issue_number)
        directory = path.parent
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise TaskIntakeReceiptError("Task metadata must be a real directory.")
            if path.exists():
                existing = TaskIntakeReceipt.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if existing.task_status in ACTIVE_TASK_STATUSES:
                    raise DuplicateTaskReceiptError(
                        "The selected Issue already has an active local reservation."
                    )
                raise TaskIntakeReceiptError(
                    "Existing task metadata requires explicit lifecycle handling."
                )
            temporary = path.with_suffix(f".json.tmp-{os.getpid()}")
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            try:
                payload = receipt.model_dump_json(indent=2).encode("utf-8") + b"\n"
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        except TaskIntakeReceiptError:
            raise
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise TaskIntakeReceiptError(
                "Task intake receipt could not be stored safely."
            ) from exc
        return path

    @contextmanager
    def lock(self, repository: Path) -> Iterator[None]:
        directory = self.directory(repository)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise TaskIntakeReceiptError("Task metadata must be a real directory.")
            lock_path = directory / ".selection.lock"
            descriptor = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as exc:
            raise TaskIntakeLockedError(
                "Another local task selection is already in progress."
            ) from exc
        except TaskIntakeReceiptError:
            raise
        except OSError as exc:
            raise TaskIntakeReceiptError(
                "Task intake lock could not be acquired safely."
            ) from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"pid={os.getpid()}\n")
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass
