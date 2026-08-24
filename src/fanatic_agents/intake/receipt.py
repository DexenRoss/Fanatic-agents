"""Atomic external storage for local task reservations."""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from fanatic_agents.intake.models import TaskIntakeReceipt, TaskStatus

METADATA_CONTAINER = ".fanatic-agents-worktrees"
LOCK_STALE_SECONDS = 60 * 60
ACTIVE_TASK_STATUSES = {
    "selected",
    "running",
    "verified",
    "promoted",
    "delivered",
    "waiting_for_ci",
    "waiting_for_review",
    "ready_for_human_merge",
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

    def load(self, repository: Path, issue_number: int) -> TaskIntakeReceipt:
        path = self.path_for(repository, issue_number)
        try:
            return TaskIntakeReceipt.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise TaskIntakeReceiptError("Task intake receipt was not found.") from exc
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise TaskIntakeReceiptError("Task intake receipt is invalid.") from exc

    def claim(self, repository: Path, issue_number: int) -> TaskIntakeReceipt:
        """Atomically advance the selected reservation to running."""
        with self.lock(repository):
            receipt = self.load(repository, issue_number)
            return self._transition_unlocked(receipt, "running")

    def transition(
        self, receipt: TaskIntakeReceipt, status: TaskStatus
    ) -> TaskIntakeReceipt:
        """Persist one monotonic lifecycle transition under the selection lock."""
        with self.lock(Path(receipt.repository)):
            current = self.load(Path(receipt.repository), receipt.issue_number)
            if current != receipt:
                raise TaskIntakeReceiptError(
                    "Task receipt changed concurrently; execution stopped closed."
                )
            return self._transition_unlocked(receipt, status)

    def _transition_unlocked(
        self, receipt: TaskIntakeReceipt, status: TaskStatus
    ) -> TaskIntakeReceipt:
        allowed = {
            "selected": {"running", "cancelled", "failed"},
            "running": {"verified", "failed"},
            "verified": {"promoted", "failed"},
            "promoted": {"delivered", "failed"},
            "delivered": {
                "waiting_for_ci", "waiting_for_review",
                "ready_for_human_merge", "merged_externally",
            },
        }
        if status not in allowed.get(receipt.task_status, set()):
            raise TaskIntakeReceiptError(
                f"Invalid task transition: {receipt.task_status} -> {status}."
            )
        updated = receipt.model_copy(update={"task_status": status})
        path = self.path_for(Path(receipt.repository), receipt.issue_number)
        temporary = path.with_suffix(f".json.tmp-{os.getpid()}")
        try:
            payload = updated.model_dump_json(indent=2).encode("utf-8") + b"\n"
            descriptor = os.open(
                temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        except OSError as exc:
            raise TaskIntakeReceiptError(
                "Task lifecycle could not be persisted atomically."
            ) from exc
        finally:
            temporary.unlink(missing_ok=True)
        return updated

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
            _recover_stale_lock(lock_path)
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
                json.dump(
                    {
                        "pid": os.getpid(),
                        "timestamp": datetime.now(UTC).isoformat(),
                    },
                    stream,
                )
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _recover_stale_lock(path: Path) -> None:
    """Remove only old locks whose recorded process is definitely gone."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        raw_timestamp = payload["timestamp"]
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(raw_timestamp, str)
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.utcoffset() is None:
            raise ValueError
    except FileNotFoundError:
        return
    except (KeyError, TypeError, ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TaskIntakeLockedError(
            "Existing task selection lock is ambiguous and was preserved."
        ) from exc

    if time.time() - timestamp.timestamp() <= LOCK_STALE_SECONDS:
        raise TaskIntakeLockedError(
            "Another local task selection may still be active."
        )
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError) as exc:
        raise TaskIntakeLockedError(
            "Existing task selection lock could not be proven orphaned."
        ) from exc
    else:
        raise TaskIntakeLockedError(
            "Another local task selection may still be active."
        )
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TaskIntakeLockedError(
            "An orphaned task selection lock could not be removed safely."
        ) from exc
