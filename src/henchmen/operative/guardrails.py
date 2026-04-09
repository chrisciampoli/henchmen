"""Guardrails – safety and observability middleware for the operative agent loop."""

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any

from henchmen.models.operative import OperativeConfig

if TYPE_CHECKING:
    from henchmen.observability.cost_accumulator import TaskCostAccumulator

logger = logging.getLogger(__name__)

# Patterns that suggest path traversal attempts
_TRAVERSAL_PATTERNS = ("../", "..\\", "/../", "\\..")

# Maximum tokens allowed in a single message (soft limit).
# Converted to chars (×4) for fast truncation without SDK calls.
_MAX_MESSAGE_TOKENS = 16_000
_MAX_MESSAGE_CHARS = _MAX_MESSAGE_TOKENS * 4  # 64K chars ≈ 16K tokens


class OperativeGuardrails:
    """Enforces safety and logging constraints on operative execution."""

    # Default per-operative cost ceiling in USD.  Can be overridden via the
    # HENCHMEN_OPERATIVE_COST_CEILING_USD environment variable.
    _DEFAULT_COST_CEILING_USD = 2.0

    # Default wall-clock ceiling in seconds for free/local providers (e.g. Ollama)
    # where USD cost is $0 and cannot serve as a stop signal.
    _DEFAULT_WALLCLOCK_CEILING_SECONDS = 1800

    def __init__(
        self,
        config: OperativeConfig,
        allowed_tools: set[str],
        max_steps: int = 20,
        task_cost_accumulator: "TaskCostAccumulator | None" = None,
    ) -> None:
        self.config = config
        self.allowed_tools = allowed_tools
        self.max_steps = max_steps
        self.tool_call_count = 0
        self.token_usage: dict[str, int] = {"input": 0, "output": 0, "cached_input": 0}
        self._step_count = 0
        self._tool_call_counts: dict[str, int] = {}
        self._nudge_count: int = 0
        self._last_input_tokens: int = 0
        self._consecutive_blocked: int = 0
        self._estimated_cost_usd: float = 0.0
        self._task_cost_accumulator = task_cost_accumulator
        self._start_time: float = time.monotonic()

        ceiling_env = os.environ.get("HENCHMEN_OPERATIVE_COST_CEILING_USD", "")
        self._cost_ceiling_usd: float = float(ceiling_env) if ceiling_env else self._DEFAULT_COST_CEILING_USD

        wallclock_env = os.environ.get("HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS", "")
        self._wallclock_ceiling_seconds: int = (
            int(wallclock_env) if wallclock_env else self._DEFAULT_WALLCLOCK_CEILING_SECONDS
        )

        # Keep strong references to fire-and-forget accumulator writes so
        # the event loop doesn't GC them mid-flight.
        self._pending_accumulator_tasks: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------
    # Pre-tool hook
    # ------------------------------------------------------------------

    def before_tool_call(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any] | None:
        """Validate a tool call before execution.

        Returns None to allow the call, or an error dict to block it.
        """
        # Check tool is in allowed set
        if tool_name not in self.allowed_tools:
            self._consecutive_blocked += 1
            logger.warning(
                "[guardrails] Blocked disallowed tool '%s' (%d consecutive) (task=%s)",
                tool_name,
                self._consecutive_blocked,
                self.config.task_id,
            )
            if self._consecutive_blocked >= 3:
                return {
                    "error": f"Tool '{tool_name}' is not permitted. "
                    f"You have tried {self._consecutive_blocked} disallowed tools in a row. "
                    f"STOP trying to use tools you don't have. "
                    f"Your available tools are: {', '.join(sorted(self.allowed_tools))}. "
                    f"Return your response as text instead."
                }
            return {"error": f"Tool '{tool_name}' is not permitted for this operative."}

        # Check for path traversal in file-related arguments using canonical resolution
        workspace = os.environ.get("WORKSPACE_DIR", "/workspace")
        path_args = [
            (k, v)
            for k, v in arguments.items()
            if isinstance(v, str) and ("path" in k.lower() or "file" in k.lower() or "dir" in k.lower())
        ]
        for arg_name, path_value in path_args:
            if not self._is_path_safe(path_value, workspace):
                logger.warning(
                    "[guardrails] Blocked path traversal attempt in tool '%s' arg '%s'='%s' (task=%s)",
                    tool_name,
                    arg_name,
                    path_value,
                    self.config.task_id,
                )
                return {"error": f"Path traversal detected in argument: '{path_value}'"}

        self._consecutive_blocked = 0  # Reset on successful tool call
        self.tool_call_count += 1
        self._tool_call_counts[tool_name] = self._tool_call_counts.get(tool_name, 0) + 1
        logger.info(
            "[guardrails] Tool call #%d: %s (task=%s)",
            self.tool_call_count,
            tool_name,
            self.config.task_id,
        )
        return None

    # ------------------------------------------------------------------
    # Post-model hook
    # ------------------------------------------------------------------

    def after_model_response(self, response: dict[str, Any]) -> None:
        """Track token usage, update running cost estimate, and log model response metadata.

        Cached input tokens are billed at 25% of the standard input rate (Vertex AI
        context caching discount), so they must be included in the ceiling check
        and accumulator — the prior implementation ignored them and under-estimated
        cost by whatever fraction of input was served from cache.
        """
        usage = response.get("usage", {})
        input_tokens = int(usage.get("input", 0) or 0)
        output_tokens = int(usage.get("output", 0) or 0)
        cached_input_tokens = int(usage.get("cached_input", 0) or 0)
        self.token_usage["input"] += input_tokens
        self.token_usage["output"] += output_tokens
        self.token_usage["cached_input"] = self.token_usage.get("cached_input", 0) + cached_input_tokens
        self._last_input_tokens = input_tokens  # Track last context size
        self._step_count += 1

        # Update running cost estimate. Pass cached_input_tokens so tracker.estimate_cost
        # applies the 0.25x cache discount rather than full-price billing.
        from henchmen.observability.tracker import estimate_cost

        step_cost = estimate_cost(
            self.config.model_name,
            input_tokens,
            output_tokens,
            cached_input_tokens=cached_input_tokens,
        )
        self._estimated_cost_usd += step_cost

        # Propagate the delta to the task-level accumulator so the ceiling
        # check reflects cumulative cost across all scheme nodes for this task.
        if self._task_cost_accumulator is not None and step_cost > 0:
            try:
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._task_cost_accumulator.add(step_cost))
                self._pending_accumulator_tasks.add(task)
                task.add_done_callback(lambda t: self._pending_accumulator_tasks.discard(t))
            except RuntimeError:
                # No running loop — we're likely being called from a sync context
                # in tests; silently skip accumulator propagation.
                pass

        content = response.get("content", [])
        tool_use_names = [p.get("name", "") for p in content if p.get("type") == "tool_use"]

        logger.info(
            "[guardrails] Model response step=%d in=%d out=%d cached=%d cost=$%.4f cumulative=$%.4f tools=%s (task=%s)",
            self._step_count,
            input_tokens,
            output_tokens,
            cached_input_tokens,
            step_cost,
            self._estimated_cost_usd,
            tool_use_names or "none",
            self.config.task_id,
        )

    # ------------------------------------------------------------------
    # Pre-model hook
    # ------------------------------------------------------------------

    def before_model_call(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Pre-process messages before sending to the model.

        Applies two optimizations:
        1. Context windowing: keep first message (task) + last N messages, drop middle.
           Must preserve tool_use/tool_result pairing — Claude rejects broken pairs.
        2. Per-message truncation for oversized string content.
        """
        from henchmen.operative.agent_builder import _CONTEXT_WINDOW_KEEP_LAST

        max_messages = _CONTEXT_WINDOW_KEEP_LAST + 1  # +1 for the initial task message
        if len(messages) > max_messages:
            tail = messages[-_CONTEXT_WINDOW_KEEP_LAST:]

            # Ensure tail starts with a user message (tool_result or text) so
            # we don't break tool_use/tool_result pairing or role alternation.
            while tail and tail[0].get("role") == "assistant" and len(tail) < len(messages) - 1:
                idx = len(messages) - len(tail) - 1
                tail = [messages[idx]] + tail

            dropped = len(messages) - len(tail)

            # Prepend the original task as a fresh user message with a context note.
            # This replaces all dropped messages with a single clean user message.
            original_task = messages[0].get("content", "")
            if isinstance(original_task, list):
                original_task = str(original_task)
            combined_first = {
                "role": "user",
                "content": (
                    f"{original_task}\n\n"
                    f"[Note: {dropped} earlier messages were trimmed to save context. "
                    f"Continue from where you left off based on the recent messages below.]"
                ),
            }
            messages = [combined_first] + tail
            logger.info(
                "[guardrails] Context window applied: dropped %d middle messages (task=%s)",
                dropped,
                self.config.task_id,
            )

        # --- Per-message truncation ---
        processed = []
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str) and len(content) > _MAX_MESSAGE_CHARS:
                logger.warning(
                    "[guardrails] Truncating oversized message (%d chars → %d) (task=%s)",
                    len(content),
                    _MAX_MESSAGE_CHARS,
                    self.config.task_id,
                )
                content = content[:_MAX_MESSAGE_CHARS] + "\n[... truncated ...]"
                msg = dict(msg, content=content)
            processed.append(msg)
        return processed

    # ------------------------------------------------------------------
    # Step-limit check
    # ------------------------------------------------------------------

    def check_step_limit(self) -> bool:
        """Return True if the step limit has been reached."""
        return self._step_count >= self.max_steps

    # ------------------------------------------------------------------
    # Cost ceiling check
    # ------------------------------------------------------------------

    def check_cost_ceiling(self) -> bool:
        """Return True if any cost / wall-clock ceiling has been exceeded.

        Checks three ceilings in order:

        1. Per-node cost ceiling (``_cost_ceiling_usd``) — the historical
           behaviour, capped at ~$2 by default.
        2. Task-level cost ceiling (``_task_cost_accumulator.ceiling_usd``)
           which spans all scheme nodes for this task. Uses the
           last-cached running total so this remains a sync hot-path;
           the underlying value is refreshed every time
           ``after_model_response`` pushes a delta.
        3. Wall-clock ceiling — a stand-in for free providers where
           ``_estimated_cost_usd`` is zero (Ollama runs fully local).
        """
        if self._estimated_cost_usd >= self._cost_ceiling_usd:
            return True

        if self._task_cost_accumulator is not None:
            task_total = self._task_cost_accumulator.cached_total_usd
            if task_total >= self._task_cost_accumulator.ceiling_usd:
                logger.warning(
                    "[guardrails] Task-level cost ceiling reached: $%.4f >= $%.4f (task=%s)",
                    task_total,
                    self._task_cost_accumulator.ceiling_usd,
                    self.config.task_id,
                )
                return True

        # Ollama / local providers return estimated_cost_usd == 0 for every
        # step, so the dollar ceiling can never fire. Substitute a wall-clock
        # ceiling to guarantee bounded execution time.
        if self._estimated_cost_usd == 0.0:
            elapsed = time.monotonic() - self._start_time
            if elapsed >= self._wallclock_ceiling_seconds:
                logger.warning(
                    "[guardrails] Wall-clock ceiling reached for free provider: %.1fs >= %ds (task=%s)",
                    elapsed,
                    self._wallclock_ceiling_seconds,
                    self.config.task_id,
                )
                return True

        return False

    # ------------------------------------------------------------------
    # Usage report
    # ------------------------------------------------------------------

    def get_usage_report(self) -> dict[str, Any]:
        """Return a summary of resource usage."""
        return {
            "steps": self._step_count,
            "tool_calls": self.tool_call_count,
            "token_usage": dict(self.token_usage),
        }

    def get_telemetry(self) -> dict[str, Any]:
        """Return comprehensive telemetry data."""
        return {
            "model_name": self.config.model_name,
            "total_input_tokens": self.token_usage.get("input", 0),
            "total_output_tokens": self.token_usage.get("output", 0),
            "model_calls": self._step_count,
            "tool_calls_count": self.tool_call_count,
            "tool_calls_by_name": dict(self._tool_call_counts),
            "tool_calls_detail": dict(self._tool_call_counts),  # backward compat
            "wall_clock_seconds": 0,  # set by caller
            "steps_used": self._step_count,
            "nudges_sent": self._nudge_count,
            "context_tokens_at_end": self._last_input_tokens,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _is_path_safe(path: str, workspace: str = "/workspace") -> bool:
        """Check if a path resolves to within the workspace using canonical resolution.

        Uses ``os.path.realpath`` so symlinks and ``..`` segments are fully
        resolved before the prefix check, preventing symlink-based escapes
        that pattern matching would miss.
        """
        resolved = os.path.realpath(os.path.join(workspace, path))
        return resolved.startswith(os.path.realpath(workspace))

    @staticmethod
    def _has_path_traversal(path: str) -> bool:
        """Return True if the path contains traversal sequences.

        .. deprecated::
            Kept for backward compatibility.  Prefer ``_is_path_safe`` which
            uses canonical resolution instead of pattern matching.
        """
        normalised = os.path.normpath(path)
        return any(pat in path for pat in _TRAVERSAL_PATTERNS) or normalised.startswith("..")
