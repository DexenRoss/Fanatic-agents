"""Atomic external scheduler state and conservative process locking."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from fanatic_agents.intake.receipt import TaskIntakeReceiptStore
from fanatic_agents.scheduler.models import SchedulerState

LOCK_STALE_SECONDS = 60 * 60


class SchedulerStateError(RuntimeError):
    """Scheduler metadata was unavailable, invalid, or ambiguous."""


class SchedulerLockedError(SchedulerStateError):
    """Another scheduler may already own this repository."""


class SchedulerStateStore:
    """Persist one scheduler state outside the managed repository."""

    def __init__(self, *, metadata_root: Path | None = None) -> None:
        self._intake = TaskIntakeReceiptStore(metadata_root=metadata_root)

    def directory(self, repository: Path) -> Path:
        return self._intake.directory(repository) / "scheduler"

    def state_path(self, repository: Path) -> Path:
        return self.directory(repository) / "state.json"

    def lock_path(self, repository: Path) -> Path:
        return self.directory(repository) / ".scheduler.lock"

    def load_or_create(
        self, repository: Path, *, now: datetime | None = None
    ) -> SchedulerState:
        resolved = Path(repository).expanduser().resolve(strict=True)
        path = self.state_path(resolved)
        current = now or datetime.now(UTC)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            state = SchedulerState(
                repository=str(resolved),
                started_at=current,
                updated_at=current,
                counter_date=current.astimezone(UTC).date(),
            )
            self.save(state)
            return state
        except (OSError, UnicodeError) as exc:
            raise SchedulerStateError(
                "Scheduler state could not be read safely."
            ) from exc
        try:
            state = SchedulerState.model_validate_json(raw)
            recorded = Path(state.repository).expanduser().resolve(strict=True)
        except (OSError, ValidationError, ValueError) as exc:
            raise SchedulerStateError(
                "Scheduler state is corrupt and was preserved."
            ) from exc
        if recorded != resolved:
            raise SchedulerStateError(
                "Scheduler state belongs to another repository and was preserved."
            )
        return state

    def load(self, repository: Path) -> SchedulerState:
        path = self.state_path(repository)
        try:
            state = SchedulerState.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            recorded = Path(state.repository).expanduser().resolve(strict=True)
            requested = Path(repository).expanduser().resolve(strict=True)
        except FileNotFoundError as exc:
            raise SchedulerStateError("Scheduler state was not found.") from exc
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise SchedulerStateError(
                "Scheduler state is corrupt and was preserved."
            ) from exc
        if recorded != requested:
            raise SchedulerStateError(
                "Scheduler state belongs to another repository and was preserved."
            )
        return state

    def save(self, state: SchedulerState) -> Path:
        path = self.state_path(Path(state.repository))
        _atomic_bytes(
            path,
            state.model_dump_json(indent=2).encode("utf-8") + b"\n",
        )
        return path

    @contextmanager
    def lock(self, repository: Path) -> Iterator[None]:
        resolved = Path(repository).expanduser().resolve(strict=True)
        directory = self.directory(resolved)
        lock_path = self.lock_path(resolved)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if directory.is_symlink() or not directory.is_dir():
                raise SchedulerStateError(
                    "Scheduler metadata must be a real external directory."
                )
            _recover_stale_lock(lock_path, resolved)
            created = datetime.now(UTC)
            descriptor = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
        except FileExistsError as exc:
            raise SchedulerLockedError(
                "Another scheduler may be active for this repository."
            ) from exc
        except SchedulerStateError:
            raise
        except OSError as exc:
            raise SchedulerStateError(
                "Scheduler lock could not be acquired safely."
            ) from exc

        try:
            payload = {
                "pid": os.getpid(),
                "timestamp": created.isoformat(),
                "repository": str(resolved),
            }
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass


def _recover_stale_lock(path: Path, repository: Path) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = payload["pid"]
        raw_timestamp = payload["timestamp"]
        raw_repository = payload["repository"]
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(raw_timestamp, str)
            or not isinstance(raw_repository, str)
            or not raw_repository
        ):
            raise ValueError
        timestamp = datetime.fromisoformat(raw_timestamp)
        recorded = Path(raw_repository).expanduser().resolve(strict=True)
        if timestamp.utcoffset() is None or recorded != repository:
            raise ValueError
    except FileNotFoundError:
        return
    except (
        KeyError,
        TypeError,
        ValueError,
        OSError,
        UnicodeError,
        json.JSONDecodeError,
    ) as exc:
        raise SchedulerLockedError(
            "Existing scheduler lock is ambiguous and was preserved."
        ) from exc

    age = time.time() - timestamp.timestamp()
    if age <= LOCK_STALE_SECONDS or _pid_may_be_active(pid):
        raise SchedulerLockedError(
            "Another scheduler may be active for this repository."
        )
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise SchedulerLockedError(
            "A clearly orphaned scheduler lock could not be removed safely."
        ) from exc


def _pid_may_be_active(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _atomic_bytes(path: Path, payload: bytes) -> None:
    temporary = path.with_suffix(f"{path.suffix}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise SchedulerStateError(
                "Scheduler metadata must be a real external directory."
            )
        descriptor = os.open(
            temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)
    except SchedulerStateError:
        raise
    except OSError as exc:
        raise SchedulerStateError(
            "Scheduler state could not be persisted atomically."
        ) from exc


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
