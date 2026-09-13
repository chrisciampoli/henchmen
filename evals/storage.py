"""Backwards-compatible shim — storage now lives in :mod:`henchmen.evals.storage`.

Importing ``evals.storage`` only works from a repo checkout; the package
module works from an installed wheel too. New code should import
``henchmen.evals.storage`` directly.
"""

from __future__ import annotations

from henchmen.evals.storage import (
    DimensionDelta,
    EvalRun,
    EvalRunSummary,
    FixtureResultRow,
    RunComparison,
    compare_runs,
    get_run,
    list_runs,
    save_run,
)

__all__ = [
    "DimensionDelta",
    "EvalRun",
    "EvalRunSummary",
    "FixtureResultRow",
    "RunComparison",
    "compare_runs",
    "get_run",
    "list_runs",
    "save_run",
]
