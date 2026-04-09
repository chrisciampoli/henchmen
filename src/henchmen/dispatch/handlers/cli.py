"""CLI task creation and embedding handlers."""

import asyncio
import logging
import os
import shutil
import tempfile
from typing import TYPE_CHECKING, Any

from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker
from henchmen.utils.git import clone_repo

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


async def handle_cli_request(
    data: dict[str, Any],
    normalizer: TaskNormalizer,
    settings: "Settings",
    broker: MessageBroker | None = None,
) -> dict[str, Any]:
    """Process a CLI task creation request."""
    task = normalizer.from_cli(data)
    msg_id = await normalizer.publish_task(task, settings, broker=broker)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}


async def handle_embed_command(
    repo: str,
    full: bool,
    settings: "Settings",
    # Legacy param kept for backward compatibility
    pinecone_api_key: str = "",
) -> dict[str, Any]:
    """Handle the 'embed' CLI command. Runs the embedding pipeline locally."""
    mode = "full" if full else "incremental"
    return await run_embedding_pipeline(
        repo=repo,
        mode=mode,
        settings=settings,
    )


async def run_embedding_pipeline(
    repo: str,
    mode: str,
    settings: "Settings",
    commit_sha: str | None = None,
    # Legacy param kept for backward compatibility
    pinecone_api_key: str = "",
) -> dict[str, Any]:
    """Run the code embedding pipeline for a repository.

    Args:
        repo: GitHub repo in "owner/repo" format
        mode: "full" (re-index everything) or "incremental" (changed files only)
        settings: Application settings
        commit_sha: For incremental mode, the commit to diff from
    """
    from henchmen.dossier.chunker import chunk_file, should_skip_file
    from henchmen.dossier.embedder import (
        delete_file_chunks,
        get_last_indexed_commit,
        set_last_indexed_commit,
        upsert_chunks,
    )

    collection_name = settings.rag_corpus_display_name
    project_id = settings.gcp_project_id
    region = settings.rag_corpus_region
    github_token = os.environ.get("GITHUB_TOKEN", "")

    logger.info("[EMBED] Starting %s embedding for %s", mode, repo)

    # Clone the repo
    tmp_dir = tempfile.mkdtemp(prefix="henchmen-embed-")
    try:
        try:
            await clone_repo(
                repo,
                "main",
                tmp_dir,
                token=github_token or None,
                depth=50,
                single_branch=False,
            )
        except RuntimeError as exc:
            return {"status": "failed", "error": str(exc)}

        # Get current HEAD sha
        proc = await asyncio.create_subprocess_exec(
            "git",
            "rev-parse",
            "HEAD",
            cwd=tmp_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        head_sha = stdout.decode().strip()

        # Determine which files to process
        if mode == "full":
            files_to_embed = _collect_all_files(tmp_dir)
            logger.info("[EMBED] Full mode, processing %d files", len(files_to_embed))
        else:
            # Incremental: diff from last indexed commit
            last_sha = commit_sha or await get_last_indexed_commit(repo, project_id=project_id)
            if not last_sha:
                logger.info("[EMBED] No last indexed commit found, falling back to full mode")
                return await run_embedding_pipeline(
                    repo=repo,
                    mode="full",
                    settings=settings,
                    commit_sha=commit_sha,
                )

            changed, deleted = await _get_changed_files(tmp_dir, last_sha, head_sha)
            if deleted:
                await delete_file_chunks(
                    repo, deleted, collection_name=collection_name, project_id=project_id, region=region
                )
                logger.info("[EMBED] Deleted chunks for %d removed files", len(deleted))
            files_to_embed = _read_files(tmp_dir, changed)
            logger.info("[EMBED] Incremental: %d changed, %d deleted files", len(changed), len(deleted))

        # Chunk all files
        all_chunks = []
        for file_path, content in files_to_embed.items():
            if not should_skip_file(file_path, file_size=len(content.encode("utf-8", errors="replace"))):
                all_chunks.extend(chunk_file(file_path, content))

        if not all_chunks:
            logger.info("[EMBED] No chunks to embed")
            await set_last_indexed_commit(repo, head_sha, project_id=project_id)
            return {"status": "completed", "chunks_upserted": 0}

        # Upsert to RAG Engine (handles embedding automatically)
        logger.info("[EMBED] Upserting %d chunks to RAG Engine...", len(all_chunks))
        count = await upsert_chunks(
            chunks=all_chunks,
            repo=repo,
            commit_sha=head_sha,
            collection_name=collection_name,
            project_id=project_id,
            region=region,
        )

        # Update last indexed commit
        await set_last_indexed_commit(repo, head_sha, project_id=project_id)

        logger.info("[EMBED] Completed: %d chunks upserted for %s@%s", count, repo, head_sha[:8])
        return {"status": "completed", "chunks_upserted": count, "commit_sha": head_sha}

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _collect_all_files(repo_dir: str) -> dict[str, str]:
    """Walk the repo and read all eligible files. Returns {rel_path: content}."""
    from henchmen.dossier.chunker import SKIP_DIRS, should_skip_file

    files: dict[str, str] = {}
    for root, dirs, filenames in os.walk(repo_dir):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for fname in filenames:
            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, repo_dir).replace("\\", "/")
            if should_skip_file(rel_path):
                continue
            try:
                with open(full_path, encoding="utf-8", errors="replace") as fh:
                    files[rel_path] = fh.read()
            except Exception:
                logger.warning("Failed to read file for embedding: %s", full_path)
    return files


async def _get_changed_files(repo_dir: str, from_sha: str, to_sha: str) -> tuple[list[str], list[str]]:
    """Get changed and deleted files between two commits. Returns (changed, deleted)."""
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "--name-status",
        from_sha,
        to_sha,
        cwd=repo_dir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    changed: list[str] = []
    deleted: list[str] = []
    for line in stdout.decode().splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0]
        if status.startswith("D"):
            deleted.append(parts[1])
        elif status.startswith("R"):
            # Rename: old_path \t new_path — delete old, add new
            deleted.append(parts[1])
            if len(parts) >= 3:
                changed.append(parts[2])
        else:
            changed.append(parts[1])
    return changed, deleted


def _read_files(repo_dir: str, file_paths: list[str]) -> dict[str, str]:
    """Read specific files from the repo. Returns {rel_path: content}."""
    files: dict[str, str] = {}
    for rel_path in file_paths:
        full_path = os.path.join(repo_dir, rel_path)
        if os.path.isfile(full_path):
            try:
                with open(full_path, encoding="utf-8", errors="replace") as fh:
                    files[rel_path] = fh.read()
            except Exception:
                logger.warning("Failed to read file: %s", full_path)
    return files
