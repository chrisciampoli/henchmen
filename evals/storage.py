"""SQLite-backed history storage for evaluation runs.

Persists eval results to ``~/.henchmen/eval/results.db`` so that runs can
be compared over time. Uses ``aiosqlite`` for async access.

Tables
------
* ``runs`` — one row per ``run_all_fixtures`` invocation.
* ``fixture_results`` — one row per fixture within a run, with dimension scores.

Public surface
--------------
* ``save_run(...)`` — persist an ``EvalRun`` (report + metadata).
* ``get_run(...)`` — retrieve a single run by ID.
* ``list_runs(...)`` — list recent runs, optionally filtered by provider.
* ``compare_runs(...)`` — dimension-by-dimension delta between two runs.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class FixtureResultRow(BaseModel):
    """Per-fixture row stored in ``fixture_results``."""

    fixture_id: str = Field(..., description="Fixture directory name")
    correctness: float = Field(default=0.0, ge=0.0, le=1.0)
    precision: float = Field(default=0.0, ge=0.0, le=1.0)
    conventions: float = Field(default=0.0, ge=0.0, le=1.0)
    efficiency: float = Field(default=0.0, ge=0.0, le=1.0)
    completion: float = Field(default=0.0, ge=0.0, le=1.0)
    wall_clock: float = Field(default=0.0, ge=0.0)
    tokens: int = Field(default=0, ge=0)
    cost: float = Field(default=0.0, ge=0.0)


class EvalRun(BaseModel):
    """A complete evaluation run with per-fixture results."""

    id: str = Field(..., description="Unique run identifier")
    provider: str = Field(..., description="LLM provider name")
    commit_sha: str | None = Field(default=None, description="Henchmen repo commit SHA")
    timestamp: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
        description="ISO-8601 timestamp",
    )
    aggregate_score: float = Field(default=0.0, ge=0.0, le=1.0)
    fixture_results: list[FixtureResultRow] = Field(default_factory=list)


class EvalRunSummary(BaseModel):
    """Lightweight summary for listing runs."""

    id: str
    provider: str
    commit_sha: str | None = None
    timestamp: str = ""
    aggregate_score: float = 0.0
    fixture_count: int = 0


class DimensionDelta(BaseModel):
    """Per-dimension delta between two runs."""

    dimension: str
    run_a_avg: float
    run_b_avg: float
    delta: float = Field(description="run_b_avg - run_a_avg; positive means improvement")


class RunComparison(BaseModel):
    """Dimension-by-dimension comparison between two runs."""

    run_a_id: str
    run_b_id: str
    run_a_aggregate: float
    run_b_aggregate: float
    aggregate_delta: float
    dimension_deltas: list[DimensionDelta] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Database path
# ---------------------------------------------------------------------------

_DEFAULT_DB_DIR = Path(os.path.expanduser("~")) / ".henchmen" / "eval"
_DEFAULT_DB_PATH = _DEFAULT_DB_DIR / "results.db"


def _db_path() -> Path:
    """Return the database path, creating the parent directory if needed."""
    override = os.environ.get("HENCHMEN_EVAL_DB_PATH")
    p = Path(override) if override else _DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_CREATE_RUNS = """
CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    provider       TEXT NOT NULL,
    commit_sha     TEXT,
    timestamp      TEXT NOT NULL,
    aggregate_score REAL NOT NULL DEFAULT 0.0
);
"""

_CREATE_FIXTURE_RESULTS = """
CREATE TABLE IF NOT EXISTS fixture_results (
    run_id       TEXT NOT NULL,
    fixture_id   TEXT NOT NULL,
    correctness  REAL NOT NULL DEFAULT 0.0,
    precision    REAL NOT NULL DEFAULT 0.0,
    conventions  REAL NOT NULL DEFAULT 0.0,
    efficiency   REAL NOT NULL DEFAULT 0.0,
    completion   REAL NOT NULL DEFAULT 0.0,
    wall_clock   REAL NOT NULL DEFAULT 0.0,
    tokens       INTEGER NOT NULL DEFAULT 0,
    cost         REAL NOT NULL DEFAULT 0.0,
    PRIMARY KEY (run_id, fixture_id),
    FOREIGN KEY (run_id) REFERENCES runs (id) ON DELETE CASCADE
);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    """Create tables if they do not exist."""
    await db.execute(_CREATE_RUNS)
    await db.execute(_CREATE_FIXTURE_RESULTS)
    await db.commit()


# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


async def save_run(run: EvalRun, *, db_path: Path | None = None) -> None:
    """Persist an ``EvalRun`` to the SQLite database."""
    path = db_path or _db_path()
    async with aiosqlite.connect(str(path)) as db:
        await _ensure_schema(db)
        await db.execute(
            "INSERT OR REPLACE INTO runs (id, provider, commit_sha, timestamp, aggregate_score) VALUES (?, ?, ?, ?, ?)",
            (run.id, run.provider, run.commit_sha, run.timestamp, run.aggregate_score),
        )
        for fr in run.fixture_results:
            await db.execute(
                "INSERT OR REPLACE INTO fixture_results "
                "(run_id, fixture_id, correctness, precision, conventions, efficiency, "
                "completion, wall_clock, tokens, cost) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    fr.fixture_id,
                    fr.correctness,
                    fr.precision,
                    fr.conventions,
                    fr.efficiency,
                    fr.completion,
                    fr.wall_clock,
                    fr.tokens,
                    fr.cost,
                ),
            )
        await db.commit()


async def get_run(run_id: str, *, db_path: Path | None = None) -> EvalRun | None:
    """Retrieve a single run by ID, or ``None`` if not found."""
    path = db_path or _db_path()
    async with aiosqlite.connect(str(path)) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        cursor = await db.execute("SELECT * FROM runs WHERE id = ?", (run_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        fixture_cursor = await db.execute(
            "SELECT * FROM fixture_results WHERE run_id = ? ORDER BY fixture_id",
            (run_id,),
        )
        fixture_rows = await fixture_cursor.fetchall()
        return EvalRun(
            id=row["id"],
            provider=row["provider"],
            commit_sha=row["commit_sha"],
            timestamp=row["timestamp"],
            aggregate_score=row["aggregate_score"],
            fixture_results=[
                FixtureResultRow(
                    fixture_id=fr["fixture_id"],
                    correctness=fr["correctness"],
                    precision=fr["precision"],
                    conventions=fr["conventions"],
                    efficiency=fr["efficiency"],
                    completion=fr["completion"],
                    wall_clock=fr["wall_clock"],
                    tokens=fr["tokens"],
                    cost=fr["cost"],
                )
                for fr in fixture_rows
            ],
        )


async def list_runs(
    provider: str | None = None,
    limit: int = 20,
    *,
    db_path: Path | None = None,
) -> list[EvalRunSummary]:
    """List recent runs, optionally filtered by provider."""
    path = db_path or _db_path()
    async with aiosqlite.connect(str(path)) as db:
        db.row_factory = aiosqlite.Row
        await _ensure_schema(db)
        if provider is not None:
            cursor = await db.execute(
                "SELECT r.*, COUNT(fr.fixture_id) AS fixture_count "
                "FROM runs r LEFT JOIN fixture_results fr ON r.id = fr.run_id "
                "WHERE r.provider = ? "
                "GROUP BY r.id ORDER BY r.timestamp DESC LIMIT ?",
                (provider, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT r.*, COUNT(fr.fixture_id) AS fixture_count "
                "FROM runs r LEFT JOIN fixture_results fr ON r.id = fr.run_id "
                "GROUP BY r.id ORDER BY r.timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
        return [
            EvalRunSummary(
                id=row["id"],
                provider=row["provider"],
                commit_sha=row["commit_sha"],
                timestamp=row["timestamp"],
                aggregate_score=row["aggregate_score"],
                fixture_count=row["fixture_count"],
            )
            for row in rows
        ]


async def compare_runs(
    run_a: str,
    run_b: str,
    *,
    db_path: Path | None = None,
) -> RunComparison:
    """Compare two runs dimension-by-dimension.

    Raises ``ValueError`` if either run is not found.
    """
    a = await get_run(run_a, db_path=db_path)
    b = await get_run(run_b, db_path=db_path)
    if a is None:
        raise ValueError(f"Run not found: {run_a}")
    if b is None:
        raise ValueError(f"Run not found: {run_b}")

    dimensions = ["correctness", "precision", "conventions", "efficiency", "completion"]
    deltas: list[DimensionDelta] = []
    for dim in dimensions:
        a_vals = [getattr(fr, dim) for fr in a.fixture_results]
        b_vals = [getattr(fr, dim) for fr in b.fixture_results]
        a_avg = sum(a_vals) / len(a_vals) if a_vals else 0.0
        b_avg = sum(b_vals) / len(b_vals) if b_vals else 0.0
        deltas.append(
            DimensionDelta(
                dimension=dim,
                run_a_avg=round(a_avg, 4),
                run_b_avg=round(b_avg, 4),
                delta=round(b_avg - a_avg, 4),
            )
        )

    return RunComparison(
        run_a_id=run_a,
        run_b_id=run_b,
        run_a_aggregate=a.aggregate_score,
        run_b_aggregate=b.aggregate_score,
        aggregate_delta=round(b.aggregate_score - a.aggregate_score, 4),
        dimension_deltas=deltas,
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
