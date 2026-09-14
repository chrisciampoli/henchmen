"""`henchmen embed` — (re)index a repository in the RAG Engine corpus from the command line.

Runs :func:`henchmen.dossier.embed_pipeline.run_embedding_pipeline` in this
process, with the same Settings the services use (``HENCHMEN_GCP_PROJECT_ID``,
``HENCHMEN_RAG_CORPUS_*``, ``HENCHMEN_GITHUB_TOKEN``). Exits non-zero unless the
run completed, so scripts and CI can rely on the exit code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

__all__ = ["add_embed_arguments", "run_embed_cli"]


def add_embed_arguments(parser: argparse.ArgumentParser) -> None:
    """Register the ``henchmen embed`` arguments."""
    parser.add_argument("repo", help="Repository to index, as owner/name")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Re-index every file (default: only files changed since the last indexed commit)",
    )


def run_embed_cli(args: argparse.Namespace) -> int:
    """Run the embedding pipeline for ``args.repo``. Returns the process exit code."""
    from henchmen.config.settings import get_settings
    from henchmen.dossier.embed_pipeline import run_embedding_pipeline

    try:
        settings = get_settings()
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        first = (str(exc).strip().splitlines() or ["invalid settings"])[0]
        print(f"ERROR: invalid configuration: {first}", file=sys.stderr)
        return 2

    mode = "full" if args.full else "incremental"
    try:
        result = asyncio.run(run_embedding_pipeline(args.repo, mode, settings))
    except Exception as exc:
        print(f"ERROR: embedding {args.repo} failed: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, default=str))
    if result.get("status") != "completed":
        print(f"ERROR: embedding {args.repo} did not complete: {result.get('error', 'unknown error')}", file=sys.stderr)
        return 1
    return 0
