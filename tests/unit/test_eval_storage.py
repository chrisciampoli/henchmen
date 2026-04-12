"""Unit tests for eval SQLite history storage."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.storage import (
    EvalRun,
    FixtureResultRow,
    compare_runs,
    get_run,
    list_runs,
    save_run,
)


def _make_run(
    run_id: str,
    provider: str = "vertex",
    aggregate: float = 0.75,
    fixtures: list[FixtureResultRow] | None = None,
) -> EvalRun:
    """Build a minimal EvalRun for testing."""
    if fixtures is None:
        fixtures = [
            FixtureResultRow(
                fixture_id="bugfix_off_by_one",
                correctness=0.8,
                precision=0.9,
                conventions=1.0,
                efficiency=0.7,
                completion=1.0,
                wall_clock=2.5,
                tokens=1500,
                cost=0.005,
            ),
            FixtureResultRow(
                fixture_id="feature_add_logging",
                correctness=0.6,
                precision=0.7,
                conventions=1.0,
                efficiency=0.8,
                completion=1.0,
                wall_clock=3.1,
                tokens=2000,
                cost=0.008,
            ),
        ]
    return EvalRun(
        id=run_id,
        provider=provider,
        commit_sha="abc123",
        aggregate_score=aggregate,
        fixture_results=fixtures,
    )


@pytest.mark.asyncio
async def test_save_and_load_round_trip(tmp_path: Path) -> None:
    """Save a run, then get it back — all fields must match."""
    db = tmp_path / "test.db"
    run = _make_run("run-001")
    await save_run(run, db_path=db)

    loaded = await get_run("run-001", db_path=db)
    assert loaded is not None
    assert loaded.id == "run-001"
    assert loaded.provider == "vertex"
    assert loaded.commit_sha == "abc123"
    assert loaded.aggregate_score == pytest.approx(0.75)
    assert len(loaded.fixture_results) == 2

    fr = loaded.fixture_results[0]
    assert fr.fixture_id == "bugfix_off_by_one"
    assert fr.correctness == pytest.approx(0.8)
    assert fr.precision == pytest.approx(0.9)
    assert fr.conventions == pytest.approx(1.0)
    assert fr.tokens == 1500


@pytest.mark.asyncio
async def test_get_run_returns_none_for_missing(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    result = await get_run("nonexistent", db_path=db)
    assert result is None


@pytest.mark.asyncio
async def test_list_runs_filters_by_provider(tmp_path: Path) -> None:
    """list_runs with a provider filter should only return matching runs."""
    db = tmp_path / "test.db"
    await save_run(_make_run("run-v1", provider="vertex"), db_path=db)
    await save_run(_make_run("run-v2", provider="vertex"), db_path=db)
    await save_run(_make_run("run-o1", provider="openai"), db_path=db)

    vertex_runs = await list_runs(provider="vertex", db_path=db)
    assert len(vertex_runs) == 2
    assert all(r.provider == "vertex" for r in vertex_runs)

    openai_runs = await list_runs(provider="openai", db_path=db)
    assert len(openai_runs) == 1
    assert openai_runs[0].provider == "openai"

    all_runs = await list_runs(db_path=db)
    assert len(all_runs) == 3


@pytest.mark.asyncio
async def test_list_runs_respects_limit(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    for i in range(10):
        await save_run(_make_run(f"run-{i:03d}"), db_path=db)

    runs = await list_runs(limit=3, db_path=db)
    assert len(runs) == 3


@pytest.mark.asyncio
async def test_list_runs_includes_fixture_count(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    await save_run(_make_run("run-001"), db_path=db)

    runs = await list_runs(db_path=db)
    assert runs[0].fixture_count == 2


@pytest.mark.asyncio
async def test_compare_runs_returns_deltas(tmp_path: Path) -> None:
    """compare_runs should produce per-dimension deltas."""
    db = tmp_path / "test.db"
    run_a = _make_run(
        "run-a",
        aggregate=0.5,
        fixtures=[
            FixtureResultRow(
                fixture_id="f1",
                correctness=0.5,
                precision=0.5,
                conventions=0.5,
                efficiency=0.5,
                completion=0.5,
            ),
        ],
    )
    run_b = _make_run(
        "run-b",
        aggregate=0.8,
        fixtures=[
            FixtureResultRow(
                fixture_id="f1",
                correctness=0.9,
                precision=0.7,
                conventions=1.0,
                efficiency=0.6,
                completion=1.0,
            ),
        ],
    )
    await save_run(run_a, db_path=db)
    await save_run(run_b, db_path=db)

    comparison = await compare_runs("run-a", "run-b", db_path=db)
    assert comparison.run_a_id == "run-a"
    assert comparison.run_b_id == "run-b"
    assert comparison.aggregate_delta == pytest.approx(0.3)

    dims = {d.dimension: d for d in comparison.dimension_deltas}
    assert dims["correctness"].delta == pytest.approx(0.4)
    assert dims["precision"].delta == pytest.approx(0.2)
    assert dims["conventions"].delta == pytest.approx(0.5)
    assert dims["completion"].delta == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_compare_runs_raises_on_missing(tmp_path: Path) -> None:
    db = tmp_path / "test.db"
    await save_run(_make_run("run-a"), db_path=db)

    with pytest.raises(ValueError, match="Run not found: run-b"):
        await compare_runs("run-a", "run-b", db_path=db)


@pytest.mark.asyncio
async def test_save_run_upsert(tmp_path: Path) -> None:
    """Saving a run with the same ID should update, not duplicate."""
    db = tmp_path / "test.db"
    await save_run(_make_run("run-001", aggregate=0.5), db_path=db)
    await save_run(_make_run("run-001", aggregate=0.9), db_path=db)

    loaded = await get_run("run-001", db_path=db)
    assert loaded is not None
    assert loaded.aggregate_score == pytest.approx(0.9)

    runs = await list_runs(db_path=db)
    assert len(runs) == 1
