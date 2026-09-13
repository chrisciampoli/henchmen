"""Backwards-compatible shim — the harness now lives in :mod:`henchmen.evals.harness`.

Importing ``evals.harness`` only works from a repo checkout; the package
module works from an installed wheel too. New code should import
``henchmen.evals.harness`` directly.
"""

from __future__ import annotations

from henchmen.evals.harness import (
    DimensionScores,
    EvalReport,
    FixtureMeta,
    FixtureResult,
    FixtureScore,
    compute_dimensions,
    run_all_fixtures,
    run_fixture,
    score_result,
)

__all__ = [
    "DimensionScores",
    "EvalReport",
    "FixtureMeta",
    "FixtureResult",
    "FixtureScore",
    "compute_dimensions",
    "run_all_fixtures",
    "run_fixture",
    "score_result",
]
