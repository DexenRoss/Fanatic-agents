"""One-shot autonomous task execution."""

from fanatic_agents.autonomous.models import (
    AutonomousRunReceipt,
    AutonomousRunResult,
)
from fanatic_agents.autonomous.service import AutonomousRunner, run_once

__all__ = [
    "AutonomousRunReceipt",
    "AutonomousRunResult",
    "AutonomousRunner",
    "run_once",
]
