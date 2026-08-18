"""Atomic external storage for the latest pull request observation snapshot."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from fanatic_agents.delivery.receipt import receipt_path_for_worktree
from fanatic_agents.observation.models import PullRequestObservation


class ObservationReceiptError(RuntimeError):
    """An observation snapshot could not be stored or parsed safely."""


def observation_path_for_worktree(worktree: Path) -> Path:
    """Locate the snapshot beside, but never inside, promotion state."""
    return receipt_path_for_worktree(worktree).with_suffix(".observation.json")


class PullRequestObservationStore:
    """Persist only the bounded normalized snapshot, never the GitHub response."""

    def save(self, observation: PullRequestObservation) -> Path:
        path = observation_path_for_worktree(Path(observation.promotion_worktree))
        metadata = path.parent
        try:
            metadata.mkdir(parents=True, exist_ok=True)
            if metadata.is_symlink() or not metadata.is_dir():
                raise ObservationReceiptError(
                    "Observation metadata must be a real directory."
                )
            temporary = path.with_suffix(f".json.tmp-{os.getpid()}")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                payload = observation.model_dump_json(indent=2).encode("utf-8") + b"\n"
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        except ObservationReceiptError:
            raise
        except OSError as exc:
            raise ObservationReceiptError(
                "The observation snapshot could not be stored safely."
            ) from exc
        return path

    def load(self, worktree: Path) -> PullRequestObservation:
        path = observation_path_for_worktree(worktree)
        try:
            return PullRequestObservation.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise ObservationReceiptError("No observation snapshot was found.") from exc
        except (OSError, UnicodeError, ValidationError, ValueError) as exc:
            raise ObservationReceiptError("The observation snapshot is invalid.") from exc
