"""Rerank RAG chunks for relevance using the configured light-tier model.

After the initial vector similarity search returns top-N chunks, this module
sends chunk summaries to a fast LLM (the ``light`` tier — Gemini Flash on
Vertex AI) to produce a task-specific relevance score. The result is a
tighter, higher-quality context window for the operative.

Graceful degradation: if the LLM call fails for any reason, the original
chunks are returned sorted by their existing relevance_score so the dossier
pipeline never breaks.

:func:`rerank_semantic_chunks` is the typed entry point: it takes and returns
``SemanticChunk`` contracts exactly as ``query_similar_chunks`` produces them,
so a caller can rerank retrieval output without converting to raw dicts.
"""

import json
import logging
from typing import TYPE_CHECKING, Any

from pydantic import Field

from henchmen.models._base import StrictBase
from henchmen.models.dossier import SemanticChunk

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.providers.interfaces.llm_provider import LLMProvider

logger = logging.getLogger(__name__)


class RerankerResult(StrictBase):
    """A reranked chunk with LLM-assigned relevance score."""

    file_path: str = Field(..., description="Relative path to the source file")
    content: str = Field(..., description="The code chunk content")
    relevance_score: float = Field(..., ge=0.0, le=1.0, description="LLM-assigned relevance (0.0-1.0)")
    original_index: int = Field(..., description="Index in the original chunk list before reranking")


_RERANK_PROMPT_TEMPLATE = """\
You are a code relevance scorer. Given a task description and a list of code \
chunks, score each chunk's relevance to the task on a scale of 0.0 to 1.0.

Task: {task_description}

Code chunks (index | file_path | preview):
{chunk_summaries}

Return a JSON array of objects with "index" (int) and "score" (float 0.0-1.0).
Only return the JSON array, no other text. Example:
[{{"index": 0, "score": 0.9}}, {{"index": 1, "score": 0.2}}]
"""


def _build_chunk_summaries(chunks: list[dict[str, Any]], preview_chars: int = 200) -> str:
    """Build a compact summary table of chunks for the reranking prompt."""
    lines: list[str] = []
    for i, chunk in enumerate(chunks):
        file_path = chunk.get("file_path", "unknown")
        content = chunk.get("content", "")
        preview = content[:preview_chars].replace("\n", " ").strip()
        lines.append(f"{i} | {file_path} | {preview}")
    return "\n".join(lines)


def _parse_rerank_response(response_text: str, num_chunks: int) -> list[tuple[int, float]]:
    """Parse the LLM JSON response into (index, score) pairs.

    Handles common malformations: markdown fences, trailing commas, partial JSON.
    Returns only valid entries with in-range indices.
    """
    # Strip markdown code fences if present
    text = response_text.strip()
    if text.startswith("```"):
        # Remove opening fence (with optional language tag) and closing fence
        lines = text.split("\n")
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    try:
        entries = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Reranker response is not valid JSON; falling back to original order")
        return []

    if not isinstance(entries, list):
        logger.warning("Reranker response is not a JSON array; falling back to original order")
        return []

    results: list[tuple[int, float]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        idx = entry.get("index")
        score = entry.get("score")
        if not isinstance(idx, int) or idx < 0 or idx >= num_chunks:
            continue
        try:
            score_f = float(score)  # type: ignore[arg-type]
            score_f = max(0.0, min(1.0, score_f))
        except (TypeError, ValueError):
            continue
        results.append((idx, score_f))

    return results


_RERANK_SYSTEM_PROMPT = "You are a code relevance scorer. Return only valid JSON."


def _resolve_model(model: str, settings: "Settings | None") -> str:
    """Resolve a model name or tier name against Settings.

    Model names are never hardcoded here (CLAUDE.md): an empty ``model``
    means "the configured light tier".
    """
    from henchmen.config.settings import get_settings
    from henchmen.models.llm import ModelTier
    from henchmen.providers.tiers import resolve_model_name

    resolved_settings = settings or get_settings()
    return resolve_model_name(resolved_settings, model or ModelTier.LIGHT.value)


async def rerank_chunks(
    chunks: list[dict[str, Any]],
    task_description: str,
    llm_provider: "LLMProvider",
    model: str = "",
    top_k: int = 10,
    settings: "Settings | None" = None,
) -> list[RerankerResult]:
    """Rerank code chunks by LLM-scored relevance to the task.

    Parameters
    ----------
    chunks:
        Raw chunk dicts with at least ``file_path`` and ``content`` keys.
    task_description:
        The full task title + description to score relevance against.
    llm_provider:
        An ``LLMProvider`` instance for making the scoring call.
    model:
        Model or tier name to score with. Empty means the configured
        ``light`` tier (cheapest).
    top_k:
        Number of top-scoring chunks to return.
    settings:
        Settings override used to resolve ``model``.

    Returns
    -------
    list[RerankerResult]
        Up to ``top_k`` chunks sorted by descending relevance score.
        On any LLM failure, returns original chunks (by existing score) as fallback.
    """
    if not chunks:
        return []

    # Build the prompt
    summaries = _build_chunk_summaries(chunks)
    prompt = _RERANK_PROMPT_TEMPLATE.format(
        task_description=task_description,
        chunk_summaries=summaries,
    )

    try:
        from henchmen.models.llm import Message, MessageRole

        response = await llm_provider.generate(
            model=_resolve_model(model, settings),
            messages=[Message(role=MessageRole.USER, content=prompt)],
            system_prompt=_RERANK_SYSTEM_PROMPT,
        )

        scored_pairs = _parse_rerank_response(response.content or "", len(chunks))

        if scored_pairs:
            # Build results from scored pairs
            results: list[RerankerResult] = []
            for idx, score in scored_pairs:
                chunk = chunks[idx]
                results.append(
                    RerankerResult(
                        file_path=chunk.get("file_path", "unknown"),
                        content=chunk.get("content", ""),
                        relevance_score=score,
                        original_index=idx,
                    )
                )
            # Sort by score descending, take top_k
            results.sort(key=lambda r: -r.relevance_score)
            return results[:top_k]

    except Exception as exc:
        logger.warning("Reranker LLM call failed, falling back to original order: %s", exc)

    # Fallback: sort by existing relevance_score and return top_k
    return _fallback_sort(chunks, top_k)


async def rerank_semantic_chunks(
    chunks: list[SemanticChunk],
    task_description: str,
    llm_provider: "LLMProvider",
    model: str = "",
    top_k: int = 10,
    settings: "Settings | None" = None,
) -> list[SemanticChunk]:
    """Rerank retrieved ``SemanticChunk`` objects and return the top ``top_k``.

    Each returned chunk is a copy of the original with ``relevance_score``
    replaced by the reranker's score (or kept, on the fallback path). Line
    spans, symbol and language survive, which ``RerankerResult`` alone drops.
    """
    if not chunks:
        return []
    raw = [
        {"file_path": chunk.file_path, "content": chunk.content, "relevance_score": chunk.relevance_score}
        for chunk in chunks
    ]
    ranked = await rerank_chunks(raw, task_description, llm_provider, model=model, top_k=top_k, settings=settings)
    return [
        chunks[result.original_index].model_copy(update={"relevance_score": result.relevance_score})
        for result in ranked
    ]


def _fallback_sort(chunks: list[dict[str, Any]], top_k: int) -> list[RerankerResult]:
    """Return chunks sorted by their existing relevance_score as a fallback."""
    results: list[RerankerResult] = []
    for i, chunk in enumerate(chunks):
        results.append(
            RerankerResult(
                file_path=chunk.get("file_path", "unknown"),
                content=chunk.get("content", ""),
                # Clamp: RerankerResult enforces 0-1 and this path runs outside
                # the try block, so an out-of-range input must not raise here.
                relevance_score=max(0.0, min(1.0, float(chunk.get("relevance_score", 0.0) or 0.0))),
                original_index=i,
            )
        )
    results.sort(key=lambda r: -r.relevance_score)
    return results[:top_k]
