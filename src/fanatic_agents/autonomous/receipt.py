"""Atomic external receipts and conservative repository-run locking."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from fanatic_agents.autonomous.models import (
    AutonomousRunReceipt,
    AutonomousTaskStatus,
    AutonomousTransition,
)
from fanatic_agents.intake.receipt import TaskIntakeReceiptStore

LOCK_STALE_SECONDS = 60 * 60
_ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "selected": {"running", "failed"},
    "running": {"verified", "failed"},
    "verified": {"promoted", "failed"},
    "promoted": {"delivered", "failed"},
    "delivered": {
        "waiting_for_ci",
        "waiting_for_review",
        "ready_for_human_merge",
        "merged_externally",
    },
    "waiting_for_ci": set(),
    "waiting_for_review": set(),
    "ready_for_human_merge": set(),
    "merged_externally": set(),
    "failed": set(),
}


class AutonomousReceiptError(RuntimeError):
    """Autonomous metadata was unavailable, invalid, or ambiguous."""


class AutonomousRunLockedError(AutonomousReceiptError):
    """Another process may own this repository's autonomous run."""


class AutonomousLock:
    """Owned lock handle whose task identity can be filled after selection."""

    def __init__(self, path: Path, created_at: datetime) -> None:
        self.path = path
        self.created_at = created_at

    def set_task_id(self, task_id: str) -> None:
        _atomic_json(
            self.path,
            {
                "pid": os.getpid(),
                "timestamp": self.created_at.isoformat(),
                "task_id": task_id,
            },
            replace=True,
        )


class AutonomousRunReceiptStore:
    """Store run state beside intake metadata, never inside the repository."""

    def __init__(self, *, metadata_root: Path | None = None) -> None:
        self._intake = TaskIntakeReceiptStore(metadata_root=metadata_root)

    def directory(self, repository: Path) -> Path:
        return self._intake.directory(repository) / "autonomous"

    def path_for(self, repository: Path, issue_number: int) -> Path:
        if issue_number <= 0:
            raise ValueError("issue_number must be greater than zero")
        return self.directory(repository) / f"issue-{issue_number}.json"

    def save(self, receipt: AutonomousRunReceipt) -> Path:
        path = self.path_for(Path(receipt.repository), receipt.issue_number)
        if path.exists():
            raise AutonomousReceiptError(
                "Existing autonomous state requires explicit recovery; the run will not repeat."
            )
        _atomic_receipt(path, receipt, replace=False)
        return path

    def load(self, repository: Path, issue_number: int) -> AutonomousRunReceipt:
        path = self.path_for(repository, issue_number)
        try:
            return AutonomousRunReceipt.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise AutonomousReceiptError("Autonomous run receipt was not found.") from exc
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise AutonomousReceiptError(
                "Autonomous run receipt is invalid; execution stopped closed."
            ) from exc

    def transition(
        self,
        receipt: AutonomousRunReceipt,
        state: AutonomousTaskStatus,
        **updates: object,
    ) -> AutonomousRunReceipt:
        if state == receipt.task_status:
            raise AutonomousReceiptError("Duplicate lifecycle transitions are not allowed.")
        if state not in _ALLOWED_TRANSITIONS[receipt.task_status]:
            raise AutonomousReceiptError(
                f"Invalid autonomous transition: {receipt.task_status} -> {state}."
            )
        now = datetime.now(UTC)
        values = receipt.model_dump()
        values.update(updates)
        values.update(
            task_status=state,
            updated_at=now,
            transitions=[
                *receipt.transitions,
                AutonomousTransition(state=state, at=now),
            ],
        )
        updated = AutonomousRunReceipt.model_validate(values)
        _atomic_receipt(
            self.path_for(Path(receipt.repository), receipt.issue_number),
            updated,
            replace=True,
        )
        return updated

    def update(
        self, receipt: AutonomousRunReceipt, **updates: object
    ) -> AutonomousRunReceipt:
        values = receipt.model_dump()
        values.update(updates)
        values["updated_at"] = datetime.now(UTC)
        updated = AutonomousRunReceipt.model_validate(values)
        _atomic_receipt(
            self.path_for(Path(receipt.repository), receipt.issue_number),
            updated,
            replace=True,
        )
        return updated

    @contextmanager
    def lock(self, repository: Path) -> Iterator[AutonomousLock]:
        directory = self.directory(repository)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise AutonomousReceiptError(
                    "Autonomous metadata must be a real external directory."
                )
            lock_path = directory / ".autonomous.lock"
            _recover_stale_lock(lock_path)
            created = datetime.now(UTC)
            descriptor = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as exc:
            raise AutonomousRunLockedError(
                "Another autonomous run may be active for this repository."
            ) from exc
        except AutonomousReceiptError:
            raise
        except OSError as exc:
            raise AutonomousReceiptError(
                "Autonomous run lock could not be acquired safely."
            ) from exc
        try:
            payload = {
                "pid": os.getpid(),
                "timestamp": created.isoformat(),
                "task_id": "pending-selection",
            }
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            yield AutonomousLock(lock_path, created)
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _recover_stale_lock(path: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        raw_timestamp = payload["timestamp"]
        task_id = payload["task_id"]
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(raw_timestamp, str)
            or not isinstance(task_id, str)
            or not task_id
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(raw_timestamp)
        if timestamp.utcoffset() is None:
            raise ValueError
    except FileNotFoundError:
        return
    except (KeyError, TypeError, ValueError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AutonomousRunLockedError(
            "Existing autonomous lock is ambiguous and was preserved."
        ) from exc

    age = time.time() - timestamp.timestamp()
    if age <= LOCK_STALE_SECONDS or _pid_may_be_active(pid):
        raise AutonomousRunLockedError(
            "Another autonomous run may be active for this repository."
        )
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise AutonomousRunLockedError(
            "A clearly orphaned autonomous lock could not be removed safely."
        ) from exc


def _pid_may_be_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _atomic_receipt(
    path: Path, receipt: AutonomousRunReceipt, *, replace: bool
) -> None:
    _atomic_bytes(
        path,
        receipt.model_dump_json(indent=2).encode("utf-8") + b"\n",
        replace=replace,
    )


def _atomic_json(path: Path, payload: dict[str, object], *, replace: bool) -> None:
    _atomic_bytes(
        path,
        (json.dumps(payload, separators=(",", ":")) + "\n").encode("utf-8"),
        replace=replace,
    )


def _atomic_bytes(path: Path, payload: bytes, *, replace: bool) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise AutonomousReceiptError("Autonomous metadata directory is unsafe.")
        temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            if replace:
                os.replace(temporary, path)
            else:
                try:
                    os.link(temporary, path)
                except FileExistsError as exc:
                    raise AutonomousReceiptError(
                        "Autonomous state already exists."
                    ) from exc
        finally:
            temporary.unlink(missing_ok=True)
    except AutonomousReceiptError:
        raise
    except OSError as exc:
        raise AutonomousReceiptError(
            "Autonomous metadata could not be persisted atomically."
        ) from exc
