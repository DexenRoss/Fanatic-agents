"""Atomic external storage for managed-service receipts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from pydantic import ValidationError

from fanatic_agents.service.models import ManagedServiceReceipt


class ServiceReceiptError(RuntimeError):
    """Managed-service metadata is absent, corrupt, or unsafe."""


def service_metadata_root(repository: Path) -> Path:
    """Return the service namespace beside, never inside, the repository."""
    resolved = Path(repository).expanduser().resolve(strict=True)
    digest = hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
    return (
        resolved.parent
        / ".fanatic-agents-worktrees"
        / ".metadata"
        / "service"
        / digest
    )


class ManagedServiceReceiptStore:
    """Persist exactly one strict receipt for each managed repository."""

    def __init__(self, *, metadata_root: Path | None = None) -> None:
        self._metadata_root = metadata_root

    def directory(self, repository: Path) -> Path:
        resolved = Path(repository).expanduser().resolve(strict=True)
        path = (
            Path(self._metadata_root).expanduser().resolve(strict=False)
            / hashlib.sha256(str(resolved).encode("utf-8")).hexdigest()
            if self._metadata_root is not None
            else service_metadata_root(resolved)
        )
        try:
            path.relative_to(resolved)
        except ValueError:
            return path
        raise ServiceReceiptError("Service metadata must remain outside the repository.")

    def path_for(self, repository: Path) -> Path:
        return self.directory(repository) / "receipt.json"

    def load(self, repository: Path) -> ManagedServiceReceipt:
        return self.load_path(self.path_for(repository), expected_repository=repository)

    def load_path(
        self, path: Path, *, expected_repository: Path | None = None
    ) -> ManagedServiceReceipt:
        target = Path(path).expanduser()
        try:
            if not target.exists():
                raise ServiceReceiptError("Managed service receipt was not found.")
            if target.is_symlink() or not target.is_file():
                raise ServiceReceiptError("Managed service receipt is not a regular file.")
            receipt = ManagedServiceReceipt.model_validate_json(
                target.read_text(encoding="utf-8")
            )
            repository = Path(receipt.repository).expanduser().resolve(strict=True)
            expected_path = self.path_for(repository).resolve(strict=False)
            if target.resolve(strict=True) != expected_path:
                raise ServiceReceiptError("Managed service receipt path is invalid.")
            if (
                expected_repository is not None
                and repository
                != Path(expected_repository).expanduser().resolve(strict=True)
            ):
                raise ServiceReceiptError(
                    "Managed service receipt belongs to another repository."
                )
            return receipt
        except ServiceReceiptError:
            raise
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise ServiceReceiptError(
                "Managed service receipt is corrupt and was preserved."
            ) from exc

    def save(
        self, receipt: ManagedServiceReceipt, *, replace: bool = False
    ) -> Path:
        path = self.path_for(Path(receipt.repository))
        if path.is_symlink():
            raise ServiceReceiptError("Refusing to replace a service receipt symlink.")
        if path.exists() and not replace:
            raise ServiceReceiptError(
                "A managed service receipt already exists; use explicit replacement."
            )
        _atomic_write(
            path, receipt.model_dump_json(indent=2).encode("utf-8") + b"\n", 0o600
        )
        return path

    def delete(self, receipt: ManagedServiceReceipt) -> None:
        path = self.path_for(Path(receipt.repository))
        current = self.load_path(path, expected_repository=Path(receipt.repository))
        if current != receipt:
            raise ServiceReceiptError("Managed service receipt changed concurrently.")
        try:
            path.unlink()
            _fsync_directory(path.parent)
        except OSError as exc:
            raise ServiceReceiptError(
                "Managed service receipt could not be removed."
            ) from exc


def atomic_write(path: Path, payload: bytes, mode: int) -> None:
    """Write bytes with a private temporary file and atomic replacement."""
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.parent.is_symlink() or not path.parent.is_dir():
            raise ServiceReceiptError("Service metadata directory is unsafe.")
        if path.is_symlink():
            raise ServiceReceiptError("Refusing to replace a service metadata symlink.")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _fsync_directory(path.parent)
    except ServiceReceiptError:
        raise
    except OSError as exc:
        raise ServiceReceiptError(
            "Managed service data could not be persisted atomically."
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


_atomic_write = atomic_write


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
