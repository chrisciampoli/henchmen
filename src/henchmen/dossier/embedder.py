"""Embedding pipeline using Vertex AI RAG Engine.

Uses RAG Engine's managed corpus for auto-embedding and semantic retrieval.
Pre-chunks code with our AST-aware chunker, then uploads each chunk as a
separate RAG file to preserve symbol boundaries.

Commit tracking metadata is stored via DocumentStore.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
from typing import TYPE_CHECKING, Any

from henchmen.models.dossier import SemanticChunk
from henchmen.providers.interfaces.document_store import DocumentStore

if TYPE_CHECKING:
    from henchmen.dossier.chunker import CodeChunk

logger = logging.getLogger(__name__)

_UPLOAD_BATCH_SIZE: int = 50  # Chunks per batch to avoid rate limits


def chunk_record_id(repo: str, file_path: str, start_line: int, end_line: int) -> str:
    """Deterministic record ID from chunk coordinates.

    Returns a 40-char hex string (truncated SHA-256).
    """
    key = f"{repo}:{file_path}:{start_line}:{end_line}"
    return hashlib.sha256(key.encode()).hexdigest()[:40]


# ---------------------------------------------------------------------------
# Corpus management
# ---------------------------------------------------------------------------


def _init_vertex(project_id: str, region: str) -> None:
    """Initialize Vertex AI SDK."""
    import vertexai  # TODO: Abstract RAG provider

    vertexai.init(project=project_id, location=region)


async def get_or_create_corpus(
    corpus_display_name: str = "henchmen-code",
    project_id: str = "",
    region: str = "us-central1",
) -> str:
    """Get an existing RAG corpus by display name, or create one.

    Returns the corpus resource name (e.g. ``projects/.../locations/.../ragCorpora/...``).
    """

    def _do() -> str:
        from vertexai import rag  # TODO: Abstract RAG provider

        _init_vertex(project_id, region)

        # Check if corpus already exists
        for corpus in rag.list_corpora():
            if corpus.display_name == corpus_display_name:
                logger.info("Found existing RAG corpus: %s", corpus.name)
                return str(corpus.name)

        # Create new corpus with text-embedding-005
        embedding_config = rag.RagEmbeddingModelConfig(
            vertex_prediction_endpoint=rag.VertexPredictionEndpoint(
                publisher_model="publishers/google/models/text-embedding-005"
            )
        )
        new_corpus = rag.create_corpus(
            display_name=corpus_display_name,
            description="AST-aware code index for Henchmen operatives",
            backend_config=rag.RagVectorDbConfig(
                rag_embedding_model_config=embedding_config,
            ),
        )
        logger.info("Created RAG corpus: %s", new_corpus.name)
        return str(new_corpus.name)

    return await asyncio.to_thread(_do)


# ---------------------------------------------------------------------------
# Upsert (upload pre-chunked code as individual RAG files)
# ---------------------------------------------------------------------------


async def upsert_chunks(
    chunks: list[CodeChunk],
    repo: str,
    commit_sha: str,
    corpus_name: str = "",
    project_id: str = "",
    region: str = "us-central1",
    # Legacy params (ignored)
    collection_name: str = "",
    pinecone_api_key: str = "",
    index_name: str = "",
) -> int:
    """Upload pre-chunked code to a RAG corpus.

    Each chunk is written to a temp file and uploaded via ``rag.upload_file``.
    The file display_name encodes metadata (repo, file_path, lines, symbol)
    so we can reconstruct it on retrieval.

    Returns the number of chunks uploaded.
    """
    if not chunks:
        return 0

    if not corpus_name:
        corpus_name = await get_or_create_corpus(
            corpus_display_name=collection_name or "henchmen-code",
            project_id=project_id,
            region=region,
        )

    def _upload_batch(batch: list[CodeChunk]) -> int:
        from vertexai import rag  # TODO: Abstract RAG provider

        _init_vertex(project_id, region)
        uploaded = 0

        for chunk in batch:
            record_id = chunk_record_id(repo, chunk.file_path, chunk.start_line, chunk.end_line)
            # Encode metadata in display_name for retrieval reconstruction
            display_name = (
                f"{record_id}|{repo}|{chunk.file_path}|{chunk.start_line}|"
                f"{chunk.end_line}|{chunk.symbol_name or ''}|{chunk.language}|{chunk.chunk_type}"
            )

            # Write chunk to temp file (RAG Engine requires file upload)
            content = f"# {chunk.file_path}\n{chunk.content[:4000]}"
            tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8")
            try:
                tmp.write(content)
                tmp.close()

                rag.upload_file(  # TODO: Abstract RAG provider
                    corpus_name=corpus_name,
                    path=tmp.name,
                    display_name=display_name,
                    description=f"{chunk.chunk_type}: {chunk.symbol_name or chunk.file_path} "
                    f"(L{chunk.start_line}-{chunk.end_line}) [{commit_sha[:8]}]",
                )
                uploaded += 1
            except Exception as exc:
                if "ALREADY_EXISTS" in str(exc):
                    uploaded += 1  # Count as success — idempotent
                else:
                    logger.warning("Failed to upload chunk %s: %s", record_id, exc)
            finally:
                os.unlink(tmp.name)

        return uploaded

    # Upload in batches with rate limiting
    total = 0
    for i in range(0, len(chunks), _UPLOAD_BATCH_SIZE):
        batch = chunks[i : i + _UPLOAD_BATCH_SIZE]
        for attempt in range(3):
            try:
                count = await asyncio.to_thread(_upload_batch, batch)
                total += count
                break
            except Exception as exc:
                if "RESOURCE_EXHAUSTED" in str(exc) or "429" in str(exc):
                    wait = (attempt + 1) * 15
                    logger.warning("Rate limited, waiting %ds (batch %d)...", wait, i)
                    await asyncio.sleep(wait)
                else:
                    logger.error("Upload batch %d failed: %s", i, exc)
                    break
        if total > 0 and total % 200 == 0:
            logger.info("Uploaded %d/%d chunks...", total, len(chunks))

    logger.info("Uploaded %d chunks to RAG corpus", total)
    return total


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def delete_file_chunks(
    repo: str,
    file_paths: list[str],
    corpus_name: str = "",
    project_id: str = "",
    region: str = "us-central1",
    # Legacy params (ignored)
    collection_name: str = "",
    pinecone_api_key: str = "",
    index_name: str = "",
) -> None:
    """Delete RAG files for the given source file paths."""
    if not corpus_name:
        try:
            corpus_name = await get_or_create_corpus(
                corpus_display_name=collection_name or "henchmen-code",
                project_id=project_id,
                region=region,
            )
        except Exception as exc:
            logger.warning("Could not get corpus for deletion: %s", exc)
            return

    file_path_set = set(file_paths)

    def _delete() -> int:
        from vertexai import rag  # TODO: Abstract RAG provider

        _init_vertex(project_id, region)
        deleted = 0
        for rag_file in rag.list_files(corpus_name=corpus_name):
            # Parse metadata from display_name
            parts = (rag_file.display_name or "").split("|")
            if len(parts) >= 3:
                file_repo = parts[1]
                file_path = parts[2]
                if file_repo == repo and file_path in file_path_set:
                    try:
                        rag.delete_file(name=rag_file.name)  # TODO: Abstract RAG provider
                        deleted += 1
                    except Exception as exc:
                        logger.warning("Failed to delete RAG file %s: %s", rag_file.name, exc)
        return deleted

    try:
        count = await asyncio.to_thread(_delete)
        logger.info("Deleted %d RAG files for %d source files", count, len(file_paths))
    except Exception as exc:
        logger.warning("RAG file deletion failed: %s", exc)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def query_similar_chunks(
    query_text: str,
    repo: str,
    corpus_name: str = "",
    project_id: str = "",
    region: str = "us-central1",
    top_k: int = 20,
    # Legacy params (ignored)
    collection_name: str = "",
    pinecone_api_key: str = "",
    index_name: str = "",
) -> list[SemanticChunk]:
    """Search for semantically similar code chunks using RAG Engine retrieval.

    The corpus handles embedding the query automatically.
    Returns an empty list on any error (graceful degradation).
    """
    if not corpus_name:
        try:
            corpus_name = await get_or_create_corpus(
                corpus_display_name=collection_name or "henchmen-code",
                project_id=project_id,
                region=region,
            )
        except Exception:
            logger.warning("Could not get corpus for query, returning empty", exc_info=True)
            return []

    def _query() -> list[SemanticChunk]:
        from vertexai import rag  # TODO: Abstract RAG provider

        _init_vertex(project_id, region)

        response = rag.retrieval_query(  # TODO: Abstract RAG provider
            text=query_text,
            rag_resources=[rag.RagResource(rag_corpus=corpus_name)],
            rag_retrieval_config=rag.RagRetrievalConfig(top_k=top_k),
        )

        chunks: list[SemanticChunk] = []
        if not response.contexts or not response.contexts.contexts:
            return chunks

        for ctx in response.contexts.contexts:
            # Try to reconstruct metadata from the source display_name
            source = getattr(ctx, "source_display_name", "") or getattr(ctx, "source_uri", "") or ""
            file_path, start_line, end_line, symbol_name, language = _parse_display_name(source, repo)
            score = float(getattr(ctx, "distance", 0.0) or 0.0)
            # RAG Engine returns distance (lower = better); convert to similarity
            relevance = max(0.0, 1.0 - score) if score > 0 else 0.5

            chunks.append(
                SemanticChunk(
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    symbol_name=symbol_name or None,
                    language=language,
                    content=ctx.text or "",
                    relevance_score=relevance,
                )
            )
        return chunks

    try:
        return await asyncio.to_thread(_query)
    except Exception:
        logger.warning("RAG retrieval failed, returning empty results", exc_info=True)
        return []


def _parse_display_name(display_name: str, default_repo: str) -> tuple[str, int, int, str, str]:
    """Parse metadata from a RAG file display_name.

    Format: ``record_id|repo|file_path|start_line|end_line|symbol_name|language|chunk_type``

    Returns (file_path, start_line, end_line, symbol_name, language).
    """
    parts = display_name.split("|")
    if len(parts) >= 7:
        try:
            return (parts[2], int(parts[3]), int(parts[4]), parts[5], parts[6])
        except (ValueError, IndexError):
            pass
    return ("unknown", 0, 0, "", "")


# ---------------------------------------------------------------------------
# Commit-tracking metadata (DocumentStore)
# ---------------------------------------------------------------------------

_METADATA_COLLECTION = "vector_search_metadata"


async def get_last_indexed_commit(
    repo: str,
    document_store: DocumentStore | None = None,
    # Legacy params (ignored)
    pinecone_api_key: str = "",
    index_name: str = "",
    project_id: str = "",
) -> str | None:
    """Read the last indexed commit SHA from DocumentStore."""
    try:
        store = document_store or _make_fallback_document_store(project_id)
        data: dict[str, Any] | None = await store.get(_METADATA_COLLECTION, repo)
        if data and "commit_sha" in data:
            return str(data["commit_sha"])
        return None
    except Exception:
        logger.warning("Failed to read last indexed commit for %s", repo, exc_info=True)
        return None


async def set_last_indexed_commit(
    repo: str,
    commit_sha: str,
    document_store: DocumentStore | None = None,
    # Legacy params (ignored)
    pinecone_api_key: str = "",
    index_name: str = "",
    project_id: str = "",
) -> None:
    """Store the last indexed commit SHA in DocumentStore."""
    try:
        store = document_store or _make_fallback_document_store(project_id)
        await store.set(_METADATA_COLLECTION, repo, {"commit_sha": commit_sha, "repo": repo})
        logger.info("Set last indexed commit for %s to %s", repo, commit_sha)
    except Exception:
        logger.warning("Failed to set last indexed commit for %s", repo, exc_info=True)


def _make_fallback_document_store(project_id: str) -> DocumentStore:
    """Create a GCP Firestore DocumentStore using a minimal settings stub.

    Used when callers don't inject a DocumentStore (backward compatibility).
    """
    from henchmen.providers.gcp.firestore import FirestoreDocumentStore

    class _MinimalSettings:
        gcp_project_id: str = project_id
        firestore_database: str = "(default)"

    return FirestoreDocumentStore(_MinimalSettings())  # type: ignore[arg-type]
