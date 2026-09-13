"""Mastermind - orchestration and task planning.

Task lifecycle state lives in Firestore ``task_executions/{task_id}``
documents, managed by :class:`SchemeExecutor` (node-level results and
checkpoints) and :class:`~henchmen.observability.tracker.TaskTracker`
(cost / cumulative metrics and heartbeat).  There is no in-memory state
machine — an earlier ``TaskStateMachine`` was decorative (built per
request, mutated, discarded, never persisted) and has been removed.
"""

# Import the shared models package FIRST. ``henchmen.models.dossier`` and
# ``henchmen.dossier.convention_detector`` reference each other, and the cycle
# only resolves when ``henchmen.models`` is the one that starts loading. Without
# this line `python -c "import henchmen.mastermind.server"` — i.e. the Cloud Run
# container's entrypoint — dies with an ImportError before FastAPI starts.
import henchmen.models  # noqa: F401  (import-order guard, not an unused import)
from henchmen.mastermind.agent import MastermindAgent
from henchmen.mastermind.lair_manager import LairManager
from henchmen.mastermind.scheme_executor import SchemeExecutor

__all__ = [
    "MastermindAgent",
    "LairManager",
    "SchemeExecutor",
]
