"""Embedding pipeline using Vertex AI RAG Engine.

Uses RAG Engine's managed corpus for auto-embedding and semantic retrieval.
Pre-chunks code with our AST-aware chunker, then uploads each chunk as a
separate RAG file to preserve symbol boundaries.

Chunk metadata (repo, file path, line span, symbol, language) travels in a
header line prepended to the uploaded chunk text, because ``RagFile``
display names are capped at 128 characters and real paths blow past that.
The display name carries only a bounded routing key so deletions can still
find every file belonging to a source path.

Commit tracking metadata is stored via the configured ``DocumentStore``.

This module is tightly coupled to Vertex AI RAG Engine: when the SDK is not
installed (``HENCHMEN_PROVIDER=local`` installs), indexing is a no-op and
retrieval returns an empty list so the dossier pipeline degrades to
grep-only context.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import logging
import os
import tempfile
from functools import lru_cache
from typing import TYPE_CHECKING, Any

from pydantic import Field

from henchmen.models._base import StrictBase
from henchmen.models.dossier import SemanticChunk
from henchmen.providers.interfaces.document_store import DocumentStore

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.dossier.chunker import CodeChunk

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _vertexai_installed() -> bool:
    """Return True when the ``vertexai`` distribution is importable.

    Uses ``find_spec`` rather than a module-level import: importing
    ``vertexai.rag`` costs tens of seconds and would be paid by every
    Mastermind cold start and every unit-test session that merely touches
    this module.
    """
    try:
        return importlib.util.find_spec("vertexai") is not None
    except (ImportError, ValueError):  # pragma: no cover — broken install
        return False


def _rag_available() -> bool:
    """Return True when the Vertex AI RAG Engine SDK is importable."""
    return _vertexai_installed()


_UPLOAD_BATCH_SIZE: int = 50  # Chunks per batch to avoid rate limits
_MAX_RETRIEVAL_TOP_K: int = 100  # Server-side cap we never exceed
_CHUNK_CONTENT_LIMIT: int = 4000  # Characters of chunk body uploaded

# Marker for the metadata header line prepended to every uploaded chunk.
_CHUNK_HEADER_PREFIX = "# henchmen-chunk|"
# Marker for the bounded display-name format (v2). Legacy files uploaded
# before the 128-character cap was honoured use the pipe-separated metadata
# format and are still parsed on retrieval and deletion.
_DISPLAY_NAME_PREFIX = "h2|"


class UpsertResult(StrictBase):
    """Outcome of an ``upsert_chunks`` call.

    ``upsert_chunks`` used to return a bare count, which callers could not
    distinguish from "RAG unavailable" or "every upload failed" — so a
    failed index run still looked like a success and the last-indexed commit
    was advanced past files whose chunks were lost.
    """

    uploaded: int = Field(default=0, description="Chunks successfully uploaded to the corpus")
    failed: int = Field(default=0, description="Chunks that could not be uploaded")
    skipped_reason: str = Field(default="", description="Why the upsert was skipped entirely, if it was")

    @property
    def ok(self) -> bool:
        """True when every requested chunk was uploaded and nothing was skipped."""
        return self.failed == 0 and not self.skipped_reason


def chunk_record_id(repo: str, file_path: str, start_line: int, end_line: int) -> str:
    """Deterministic record ID from chunk coordinates.

    Returns a 40-char hex string (truncated SHA-256).
    """
    key = f"{repo}:{file_path}:{start_line}:{end_line}"
    return hashlib.sha256(key.encode()).hexdigest()[:40]


def source_path_key(repo: str, file_path: str) -> str:
    """Bounded routing key for every chunk of one source file in one repo."""
    return hashlib.sha256(f"{repo}:{file_path}".encode()).hexdigest()[:32]


def _resolve_settings(settings: Settings | None) -> Settings:
    """Return the supplied settings or the process-wide singleton."""
    if settings is not None:
        return settings
    from henchmen.config.settings import get_settings

    return get_settings()


def _corpus_defaults(
    settings: Settings | None,
    corpus_display_name: str,
    project_id: str,
    region: str,
) -> tuple[str, str, str]:
    """Fill empty corpus coordinates from Settings (never from literals)."""
    if corpus_display_name and project_id and region:
        return corpus_display_name, project_id, region
    try:
        resolved = _resolve_settings(settings)
    except Exception:  # pragma: no cover — misconfigured env; keep caller values
        logger.warning("Could not load Settings for RAG corpus defaults", exc_info=True)
        return corpus_display_name, project_id, region
    return (
        corpus_display_name or resolved.rag_corpus_display_name,
        project_id or resolved.gcp_project_id,
        region or resolved.rag_corpus_region,
    )


# ---------------------------------------------------------------------------
# Chunk metadata encoding
# ---------------------------------------------------------------------------


def build_display_name(repo: str, chunk: CodeChunk) -> str:
    """Return the RagFile display name for a chunk.

    ``RagFile.display_name`` is capped at 128 characters by the API, so the
    name carries only two bounded hashes: the source-path key (used by
    deletion) and the chunk record id (used for idempotency/debugging).
    Human-readable metadata lives in the chunk header instead.
    """
    path_key = source_path_key(repo, chunk.file_path)
    record_id = chunk_record_id(repo, chunk.file_path, chunk.start_line, chunk.end_line)
    return f"{_DISPLAY_NAME_PREFIX}{path_key}|{record_id}"


def build_chunk_payload(repo: str, chunk: CodeChunk) -> str:
    """Return the text uploaded for a chunk: metadata header + source body."""
    header = (
        f"{_CHUNK_HEADER_PREFIX}{repo}|{chunk.file_path}|{chunk.start_line}|"
        f"{chunk.end_line}|{chunk.symbol_name or ''}|{chunk.language}|{chunk.chunk_type}"
    )
    return f"{header}\n{chunk.content[:_CHUNK_CONTENT_LIMIT]}"


def _parse_chunk_header(text: str) -> tuple[str, str, int, int, str, str] | None:
    """Parse ``(repo, file_path, start, end, symbol, language)`` from chunk text."""
    if not text:
        return None
    first_line = text.split("\n", 1)[0]
    if not first_line.startswith(_CHUNK_HEADER_PREFIX):
        return None
    parts = first_line[len(_CHUNK_HEADER_PREFIX) :].split("|")
    if len(parts) < 6:
        return None
    try:
        return (parts[0], parts[1], int(parts[2]), int(parts[3]), parts[4], parts[5])
    except ValueError:
        return None


def _strip_chunk_header(text: str) -> str:
    """Return chunk text with the metadata header line removed."""
    if text.startswith(_CHUNK_HEADER_PREFIX):
        _, _, rest = text.partition("\n")
        return rest
    return text


def _parse_display_name(display_name: str, default_repo: str) -> tuple[str, str, int, int, str, str] | None:
    """Parse legacy pipe-separated metadata from a RAG file display name.

    Legacy format: ``record_id|repo|file_path|start|end|symbol|language|chunk_type``.
    Returns ``(repo, file_path, start_line, end_line, symbol_name, language)``
    or ``None`` when the name carries no metadata (v2 names, or garbage).
    """
    if not display_name or display_name.startswith(_DISPLAY_NAME_PREFIX):
        return None
    parts = display_name.split("|")
    if len(parts) >= 7:
        try:
            return (parts[1] or default_repo, parts[2], int(parts[3]), int(parts[4]), parts[5], parts[6])
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Corpus management
# ---------------------------------------------------------------------------


def _init_vertex(project_id: str, region: str) -> None:
    """Initialize Vertex AI SDK."""
    import vertexai

    vertexai.init(project=project_id, location=region)


# Resolved corpus resource names, keyed by (project_id, region, display_name).
# A corpus resource name never changes, so caching avoids an O(corpora)
# list_corpora round-trip on every task.
_CORPUS_CACHE: dict[tuple[str, str, str], str] = {}


def _reset_corpus_cache() -> None:
    """Clear the resolved-corpus cache (tests and long-lived processes)."""
    _CORPUS_CACHE.clear()


async def get_corpus(
    corpus_display_name: str = "",
    project_id: str = "",
    region: str = "",
    settings: Settings | None = None,
) -> str:
    """Look up an existing RAG corpus by display name without creating one.

    Returns the corpus resource name, or an empty string when it does not
    exist or the Vertex AI RAG Engine SDK is unavailable. Read paths must use
    this rather than ``get_or_create_corpus`` so a query never creates an
    empty corpus as a side effect.
    """
    if not _rag_available():
        logger.warning("Vertex AI RAG is not available — dossier will run in grep-only mode (get_corpus is a no-op)")
        return ""

    corpus_display_name, project_id, region = _corpus_defaults(settings, corpus_display_name, project_id, region)
    cache_key = (project_id, region, corpus_display_name)
    cached = _CORPUS_CACHE.get(cache_key)
    if cached:
        return cached

    def _do() -> str:
        from vertexai import rag

        _init_vertex(project_id, region)
        for corpus in rag.list_corpora():
            if corpus.display_name == corpus_display_name:
                return str(corpus.name)
        return ""

    name = await asyncio.to_thread(_do)
    if name:
        _CORPUS_CACHE[cache_key] = name
    return name


async def get_or_create_corpus(
    corpus_display_name: str = "",
    project_id: str = "",
    region: str = "",
    embedding_model: str = "",
    settings: Settings | None = None,
) -> str:
    """Get an existing RAG corpus by display name, or create one.

    Returns the corpus resource name (e.g. ``projects/.../locations/.../ragCorpora/...``).
    Returns an empty string when the Vertex AI RAG Engine SDK is unavailable
    (e.g. local mode), allowing callers to gracefully skip RAG operations.
    Only the indexing path should call this — reads use :func:`get_corpus`.
    """
    if not _rag_available():
        logger.warning(
            "Vertex AI RAG is not available — dossier will run in grep-only mode (get_or_create_corpus is a no-op)"
        )
        return ""

    corpus_display_name, project_id, region = _corpus_defaults(settings, corpus_display_name, project_id, region)
    if not embedding_model:
        try:
            embedding_model = _resolve_settings(settings).rag_embedding_model
        except Exception:  # pragma: no cover — misconfigured env
            logger.warning("Could not load Settings for RAG embedding model", exc_info=True)

    existing = await get_corpus(corpus_display_name, project_id, region, settings=settings)
    if existing:
        logger.info("Found existing RAG corpus: %s", existing)
        return existing

    if not embedding_model:
        logger.error("No RAG embedding model configured (HENCHMEN_RAG_EMBEDDING_MODEL); cannot create corpus")
        return ""

    def _do() -> str:
        from vertexai import rag

        _init_vertex(project_id, region)

        embedding_config = rag.RagEmbeddingModelConfig(
            vertex_prediction_endpoint=rag.VertexPredictionEndpoint(
                publisher_model=f"publishers/google/models/{embedding_model}"
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

    name = await asyncio.to_thread(_do)
    if name:
        _CORPUS_CACHE[(project_id, region, corpus_display_name)] = name
    return name


# ---------------------------------------------------------------------------
# Upsert (upload pre-chunked code as individual RAG files)
# ---------------------------------------------------------------------------


def _is_rate_limit(exc: BaseException) -> bool:
    """True when an exception looks like a Vertex AI quota / rate-limit error."""
    text = str(exc)
    return "RESOURCE_EXHAUSTED" in text or "429" in text


async def upsert_chunks(
    chunks: list[CodeChunk],
    repo: str,
    commit_sha: str,
    corpus_name: str = "",
    project_id: str = "",
    region: str = "",
    collection_name: str = "",
    replace_existing: bool = False,
    replace_files: bool = True,
    settings: Settings | None = None,
) -> UpsertResult:
    """Upload pre-chunked code to a RAG corpus.

    Each chunk is written to a temp file and uploaded via ``rag.upload_file``.
    Chunk metadata travels in the uploaded text's header line so retrieval can
    reconstruct it without exceeding the 128-character display-name cap.

    Args:
        chunks: Pre-chunked source code.
        repo: ``owner/repo`` slug the chunks belong to.
        commit_sha: Commit the chunks were produced from (recorded in the
            RagFile description for debugging).
        corpus_name: Resolved corpus resource name; looked up when empty.
        project_id: GCP project; defaults to ``settings.gcp_project_id``.
        region: RAG corpus region; defaults to ``settings.rag_corpus_region``.
        collection_name: Corpus *display* name; defaults to
            ``settings.rag_corpus_display_name``.
        replace_existing: Delete every RagFile already indexed for ``repo``
            before uploading. Required for a full re-index, which would
            otherwise duplicate every chunk.
        replace_files: Delete the chunks already indexed for every source
            file present in ``chunks`` before uploading (ignored when
            ``replace_existing`` already cleared the repo). ``rag.upload_file``
            always creates a new server-side file, so re-embedding a modified
            file without this leaves its stale chunks searchable alongside the
            new ones. If the upload then fails the result is not ``ok``, the
            caller does not advance the last-indexed commit, and the next run
            re-embeds the file.
        settings: Settings override (defaults to the process singleton).

    Returns:
        An :class:`UpsertResult` distinguishing success, partial failure and
        "skipped because RAG is unavailable".
    """
    if not chunks:
        return UpsertResult()

    if not _rag_available():
        logger.warning(
            "Vertex AI RAG is not available — skipping upsert of %d chunks (grep-only mode)",
            len(chunks),
        )
        return UpsertResult(skipped_reason="vertex-ai-rag-unavailable")

    corpus_display_name, project_id, region = _corpus_defaults(settings, collection_name, project_id, region)

    if not corpus_name:
        corpus_name = await get_or_create_corpus(
            corpus_display_name=corpus_display_name,
            project_id=project_id,
            region=region,
            settings=settings,
        )
    if not corpus_name:
        return UpsertResult(skipped_reason="rag-corpus-unavailable")

    if replace_existing:
        await delete_repo_chunks(
            repo,
            corpus_name=corpus_name,
            project_id=project_id,
            region=region,
            collection_name=corpus_display_name,
            settings=settings,
        )
    elif replace_files:
        file_paths = sorted({chunk.file_path for chunk in chunks})
        try:
            removed = await _delete_matching(repo, corpus_name, project_id, region, file_paths)
        except Exception:
            # Uploading anyway would leave stale chunks searchable next to the
            # new ones while reporting success; fail so the commit is not advanced.
            logger.error("Could not clear existing chunks for %d files in %s", len(file_paths), repo, exc_info=True)
            return UpsertResult(failed=len(chunks), skipped_reason="stale-chunk-delete-failed")
        logger.info("Cleared %d existing RAG files for %d re-embedded source files", removed, len(file_paths))

    def _upload_batch(batch: list[CodeChunk]) -> tuple[int, list[CodeChunk], int]:
        """Upload a batch.

        Returns ``(uploaded, remaining_after_rate_limit, permanently_failed)``.
        Rate-limit errors stop the batch and hand the remaining chunks back so
        the caller can back off and retry them — previously they were caught
        per chunk here, which made the retry loop unreachable.
        """
        from vertexai import rag

        _init_vertex(project_id, region)
        uploaded = 0
        failed = 0

        for index, chunk in enumerate(batch):
            display_name = build_display_name(repo, chunk)
            content = build_chunk_payload(repo, chunk)

            # Write chunk to temp file (RAG Engine requires file upload).
            # Use delete=False + manual unlink so the file survives the with
            # block until rag.upload_file has finished reading it.
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".txt",
                delete=False,
                encoding="utf-8",
            ) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            try:
                rag.upload_file(
                    corpus_name=corpus_name,
                    path=tmp_path,
                    display_name=display_name,
                    description=f"{chunk.chunk_type}: {chunk.symbol_name or chunk.file_path} "
                    f"(L{chunk.start_line}-{chunk.end_line}) [{commit_sha[:8]}]",
                )
                uploaded += 1
            except Exception as exc:
                if _is_rate_limit(exc):
                    return uploaded, batch[index:], failed
                failed += 1
                logger.warning("Failed to upload chunk %s: %s", display_name, exc)
            finally:
                os.unlink(tmp_path)

        return uploaded, [], failed

    total = 0
    total_failed = 0
    for i in range(0, len(chunks), _UPLOAD_BATCH_SIZE):
        pending = chunks[i : i + _UPLOAD_BATCH_SIZE]
        for attempt in range(3):
            try:
                count, pending, failed = await asyncio.to_thread(_upload_batch, pending)
            except Exception as exc:
                logger.error("Upload batch %d failed: %s", i, exc)
                total_failed += len(pending)
                pending = []
                break
            total += count
            total_failed += failed
            if not pending:
                break
            wait = (attempt + 1) * 15
            logger.warning("Rate limited, waiting %ds before retrying %d chunks (batch %d)", wait, len(pending), i)
            await asyncio.sleep(wait)
        if pending:
            logger.error("Giving up on %d chunks in batch %d after rate-limit retries", len(pending), i)
            total_failed += len(pending)
        if total > 0 and total % 200 == 0:
            logger.info("Uploaded %d/%d chunks...", total, len(chunks))

    if total_failed:
        logger.error("Uploaded %d chunks to RAG corpus; %d chunks failed", total, total_failed)
    else:
        logger.info("Uploaded %d chunks to RAG corpus", total)
    return UpsertResult(uploaded=total, failed=total_failed)


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------


async def _delete_matching(
    repo: str,
    corpus_name: str,
    project_id: str,
    region: str,
    file_paths: list[str] | None,
) -> int:
    """Delete RagFiles for ``repo``; when ``file_paths`` is None, delete all."""
    path_keys = {source_path_key(repo, path) for path in file_paths} if file_paths is not None else None
    path_set = set(file_paths) if file_paths is not None else None

    def _delete() -> int:
        from vertexai import rag

        _init_vertex(project_id, region)
        deleted = 0
        for rag_file in rag.list_files(corpus_name=corpus_name):
            display_name = rag_file.display_name or ""
            matches = False
            if display_name.startswith(_DISPLAY_NAME_PREFIX):
                parts = display_name.split("|")
                if len(parts) >= 2:
                    matches = path_keys is None or parts[1] in path_keys
            else:
                legacy = _parse_display_name(display_name, repo)
                if legacy is not None:
                    file_repo, file_path = legacy[0], legacy[1]
                    matches = file_repo == repo and (path_set is None or file_path in path_set)
            if not matches:
                continue
            try:
                rag.delete_file(name=rag_file.name)
                deleted += 1
            except Exception as exc:
                logger.warning("Failed to delete RAG file %s: %s", rag_file.name, exc)
        return deleted

    return await asyncio.to_thread(_delete)


async def delete_file_chunks(
    repo: str,
    file_paths: list[str],
    corpus_name: str = "",
    project_id: str = "",
    region: str = "",
    collection_name: str = "",
    settings: Settings | None = None,
) -> None:
    """Delete RAG files for the given source file paths.

    No-op when the Vertex AI RAG Engine SDK is unavailable (local mode) or
    when the corpus does not exist — a delete must never create one.
    """
    if not _rag_available():
        logger.warning(
            "Vertex AI RAG is not available — skipping delete for %d files (grep-only mode)",
            len(file_paths),
        )
        return

    corpus_display_name, project_id, region = _corpus_defaults(settings, collection_name, project_id, region)

    if not corpus_name:
        try:
            corpus_name = await get_corpus(
                corpus_display_name=corpus_display_name,
                project_id=project_id,
                region=region,
                settings=settings,
            )
        except Exception as exc:
            logger.warning("Could not get corpus for deletion: %s", exc)
            return
    if not corpus_name:
        logger.info("No RAG corpus %r; nothing to delete", corpus_display_name)
        return

    try:
        count = await _delete_matching(repo, corpus_name, project_id, region, file_paths)
        logger.info("Deleted %d RAG files for %d source files", count, len(file_paths))
    except Exception as exc:
        logger.warning("RAG file deletion failed: %s", exc)


async def delete_repo_chunks(
    repo: str,
    corpus_name: str = "",
    project_id: str = "",
    region: str = "",
    collection_name: str = "",
    settings: Settings | None = None,
) -> None:
    """Delete every indexed RagFile belonging to ``repo``.

    Used before a full re-index; without it a re-index duplicates every chunk
    because ``rag.upload_file`` always creates a new server-side file.
    """
    if not _rag_available():
        return

    corpus_display_name, project_id, region = _corpus_defaults(settings, collection_name, project_id, region)

    if not corpus_name:
        try:
            corpus_name = await get_corpus(
                corpus_display_name=corpus_display_name,
                project_id=project_id,
                region=region,
                settings=settings,
            )
        except Exception as exc:
            logger.warning("Could not get corpus for repo deletion: %s", exc)
            return
    if not corpus_name:
        return

    try:
        count = await _delete_matching(repo, corpus_name, project_id, region, None)
        logger.info("Deleted %d RAG files for %s before re-index", count, repo)
    except Exception as exc:
        logger.warning("RAG repo deletion failed: %s", exc)


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


async def query_similar_chunks(
    query_text: str,
    repo: str,
    corpus_name: str = "",
    project_id: str = "",
    region: str = "",
    top_k: int = 20,
    collection_name: str = "",
    settings: Settings | None = None,
) -> list[SemanticChunk]:
    """Search for semantically similar code chunks using RAG Engine retrieval.

    The corpus handles embedding the query automatically. Results from other
    repositories sharing the corpus are dropped, so an operative is never
    shown code from a repo it is not working on.

    Returns an empty list on any error or when the Vertex AI RAG Engine SDK
    is unavailable (graceful degradation).
    """
    if not _rag_available():
        logger.warning("Vertex AI RAG is not available — returning empty retrieval result (grep-only mode)")
        return []

    corpus_display_name, project_id, region = _corpus_defaults(settings, collection_name, project_id, region)

    if not corpus_name:
        try:
            corpus_name = await get_corpus(
                corpus_display_name=corpus_display_name,
                project_id=project_id,
                region=region,
                settings=settings,
            )
        except Exception:
            logger.warning("Could not get corpus for query, returning empty", exc_info=True)
            return []
    if not corpus_name:
        logger.info("No RAG corpus %r; returning empty retrieval result", corpus_display_name)
        return []

    # Over-fetch so the client-side repo filter still leaves top_k results.
    fetch_k = min(max(top_k * 3, top_k), _MAX_RETRIEVAL_TOP_K)

    def _query() -> list[SemanticChunk]:
        from vertexai import rag

        _init_vertex(project_id, region)

        response = rag.retrieval_query(
            text=query_text,
            rag_resources=[rag.RagResource(rag_corpus=corpus_name)],
            rag_retrieval_config=rag.RagRetrievalConfig(top_k=fetch_k),
        )

        chunks: list[SemanticChunk] = []
        if not response.contexts or not response.contexts.contexts:
            return chunks

        for ctx in response.contexts.contexts:
            text = ctx.text or ""
            meta = _parse_chunk_header(text)
            if meta is None:
                source = getattr(ctx, "source_display_name", "") or getattr(ctx, "source_uri", "") or ""
                meta = _parse_display_name(source, repo)
            if meta is None:
                logger.debug("Dropping RAG context with unparseable metadata")
                continue

            chunk_repo, file_path, start_line, end_line, symbol_name, language = meta
            if chunk_repo != repo:
                # Every repo shares one corpus; foreign chunks point at files
                # that do not exist in this workspace.
                continue

            # RAG Engine reports ``score`` (a distance — lower is better).
            # There is no ``distance`` attribute on the context object.
            score = float(getattr(ctx, "score", 0.0) or 0.0)
            relevance = max(0.0, min(1.0, 1.0 - score)) if score > 0 else 0.5

            chunks.append(
                SemanticChunk(
                    file_path=file_path,
                    start_line=start_line,
                    end_line=end_line,
                    symbol_name=symbol_name or None,
                    language=language,
                    content=_strip_chunk_header(text),
                    relevance_score=relevance,
                )
            )
        return chunks[:top_k]

    try:
        return await asyncio.to_thread(_query)
    except Exception:
        logger.warning("RAG retrieval failed, returning empty results", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Commit-tracking metadata (DocumentStore)
# ---------------------------------------------------------------------------

_METADATA_COLLECTION = "vector_search_metadata"


def _default_document_store(settings: Settings | None, project_id: str) -> DocumentStore:
    """Build the DocumentStore configured for this deployment.

    Previously hardwired to Firestore, which silently failed under
    ``HENCHMEN_PROVIDER=local`` (SQLite) and ``aws`` (DynamoDB): the import
    error was swallowed, so every incremental index run became a full run and
    commit SHAs were dropped.
    """
    from henchmen.providers.registry import ProviderRegistry

    resolved = _resolve_settings(settings)
    if project_id and not resolved.gcp_project_id:
        resolved = resolved.model_copy(update={"gcp_project_id": project_id})
    return ProviderRegistry(resolved).get_document_store()


async def get_last_indexed_commit(
    repo: str,
    document_store: DocumentStore | None = None,
    settings: Settings | None = None,
    project_id: str = "",
) -> str | None:
    """Read the last indexed commit SHA from the configured DocumentStore."""
    try:
        store = document_store or _default_document_store(settings, project_id)
        data: dict[str, Any] | None = await store.get(_METADATA_COLLECTION, repo)
        if data and "commit_sha" in data:
            return str(data["commit_sha"])
        return None
    except Exception:
        logger.error("Failed to read last indexed commit for %s", repo, exc_info=True)
        return None


async def set_last_indexed_commit(
    repo: str,
    commit_sha: str,
    document_store: DocumentStore | None = None,
    settings: Settings | None = None,
    project_id: str = "",
) -> None:
    """Store the last indexed commit SHA in the configured DocumentStore."""
    try:
        store = document_store or _default_document_store(settings, project_id)
        await store.set(_METADATA_COLLECTION, repo, {"commit_sha": commit_sha, "repo": repo})
        logger.info("Set last indexed commit for %s to %s", repo, commit_sha)
    except Exception:
        logger.error("Failed to set last indexed commit for %s", repo, exc_info=True)
