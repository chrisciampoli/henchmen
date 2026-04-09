"""Mastermind - orchestration and task planning.

Task lifecycle state lives in Firestore ``task_executions/{task_id}``
documents, managed by :class:`SchemeExecutor` (node-level results and
checkpoints) and :class:`~henchmen.observability.tracker.TaskTracker`
(cost / cumulative metrics and heartbeat).  There is no in-memory state
machine — an earlier ``TaskStateMachine`` was decorative (built per
request, mutated, discarded, never persisted) and has been removed.
"""

from henchmen.mastermind.agent import MastermindAgent
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor

__all__ = [
    "MastermindAgent",
    "LairManager",
    "SchemeExecutor",
]
