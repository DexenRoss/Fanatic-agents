"""External atomic storage and locking for promotion receipts."""

from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pydantic import ValidationError

from fanatic_agents.delivery.models import PromotionReceipt

WORKTREE_CONTAINER = ".fanatic-agents-worktrees"
LOCK_STALE_SECONDS = 60 * 60


class ReceiptError(RuntimeError):
    """A receipt could not be located, parsed, or stored safely."""


class DeliveryLockedError(ReceiptError):
    """Another local delivery currently owns the promotion lock."""


def repository_identifier(repository: Path) -> str:
    """Create a stable local identifier without exposing repository content."""
    resolved = Path(repository).expanduser().resolve(strict=True)
    return hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()


def metadata_root_for_worktree(worktree: Path) -> Path:
    """Find the external metadata directory belonging to a promotion path."""
    candidate = Path(worktree).expanduser().resolve(strict=False)
    for parent in (candidate.parent, *candidate.parents):
        if parent.name == WORKTREE_CONTAINER:
            return parent / ".metadata"
    raise ReceiptError("The path is not inside a Fanatic Agents worktree container.")


def receipt_path_for_worktree(worktree: Path) -> Path:
    """Return a traversal-safe receipt filename derived from the canonical path."""
    resolved = Path(worktree).expanduser().resolve(strict=False)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return metadata_root_for_worktree(resolved) / f"{digest}.json"


class PromotionReceiptStore:
    """Persist one strict receipt outside both source repository and worktree."""

    def save(self, receipt: PromotionReceipt) -> Path:
        path = receipt_path_for_worktree(Path(receipt.worktree_path))
        metadata = path.parent
        try:
            metadata.mkdir(parents=True, exist_ok=True)
            if metadata.is_symlink() or not metadata.is_dir():
                raise ReceiptError("Promotion metadata must be a real directory.")
            temporary = path.with_suffix(f".json.tmp-{os.getpid()}")
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            descriptor = os.open(temporary, flags, 0o600)
            try:
                payload = receipt.model_dump_json(indent=2).encode("utf-8") + b"\n"
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except ReceiptError:
            raise
        except OSError as exc:
            raise ReceiptError("Promotion receipt could not be stored safely.") from exc
        return path

    def load(self, worktree: Path) -> PromotionReceipt:
        path = receipt_path_for_worktree(worktree)
        try:
            raw = path.read_text(encoding="utf-8")
            return PromotionReceipt.model_validate_json(raw)
        except FileNotFoundError as exc:
            raise ReceiptError("No Fanatic Agents promotion receipt was found.") from exc
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise ReceiptError("The Fanatic Agents promotion receipt is invalid.") from exc

    @contextmanager
    def lock(self, worktree: Path) -> Iterator[None]:
        receipt_path = receipt_path_for_worktree(worktree)
        metadata = receipt_path.parent
        metadata.mkdir(parents=True, exist_ok=True)
        if metadata.is_symlink() or not metadata.is_dir():
            raise ReceiptError("Promotion metadata must be a real directory.")
        lock_path = receipt_path.with_suffix(".lock")
        self._remove_stale_lock(lock_path)
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise DeliveryLockedError(
                "Another delivery operation is active for this promotion worktree."
            ) from exc
        except OSError as exc:
            raise ReceiptError("The delivery lock could not be acquired safely.") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(f"pid={os.getpid()}\ncreated={int(time.time())}\n")
            yield
        finally:
            try:
                lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _remove_stale_lock(lock_path: Path) -> None:
        try:
            age = time.time() - lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReceiptError("The delivery lock could not be inspected safely.") from exc
        if age <= LOCK_STALE_SECONDS:
            return
        try:
            lock_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise ReceiptError("A stale delivery lock could not be removed safely.") from exc
