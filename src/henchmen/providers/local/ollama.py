"""Ollama implementation of LLMProvider for local development."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from henchmen.models.llm import LLMResponse, Message, ModelTier, TokenUsage, ToolCall, ToolDefinition

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)


class OllamaProvider:
    """LLMProvider backed by a local Ollama server.

    .. note::
       BYO-LLM via Ollama is experimental. Scheme nodes that reference cloud
       model names (e.g. ``gemini-2.5-pro``) are remapped to a local Ollama
       model. This flattens the scheme's model tiering — a task that intended
       to use a reasoning-heavy model and a lightweight model will run both
       nodes on the same local model. A warning is logged on every remap so
       that degraded parity is visible to the operator.

       For best results, use an Ollama model with native tool-calling support
       (e.g. ``qwen2.5-coder:7b`` or ``llama3.3``). Models like ``llama3.2``
       have known weaknesses around function calling under Ollama.
    """

    # Map cloud model name prefixes or tier names to recommended local Ollama
    # models. The operator can override any of these via env vars of the form
    # ``HENCHMEN_LLM_OLLAMA_MODEL_<TIER_NAME>`` (e.g.
    # ``HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX=qwen2.5-coder:7b``). Future work: wire
    # these into Settings with proper fields.
    _TIER_HINTS: dict[str, str] = {
        "COMPLEX": "qwen2.5-coder:7b (tool-calling capable, strong for code)",
        "LIGHT": "qwen2.5:3b (smaller, faster for planning/analysis)",
        "REASONING": "deepseek-r1:8b (reasoning-heavy tasks like fix_tests)",
    }

    def __init__(self, settings: Settings) -> None:
        self._base_url = getattr(settings, "llm_ollama_base_url", "http://localhost:11434")
        self._default_model = getattr(settings, "llm_ollama_model", "llama3.2")
        self._skip_probe = bool(getattr(settings, "llm_ollama_skip_probe", False))
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)
        # Track which tier flattens we've already warned about to avoid log noise.
        self._warned_tiers: set[str] = set()
        # C3 capability probe state: None → not probed, "ok" → probed OK,
        # "skipped" → short-circuited via llm_ollama_skip_probe, "failed"
        # → the probe detected the model cannot emit tool calls.
        self._tool_probe_state: str | None = None

    def resolve_tier(self, tier: str) -> str:
        """Map any model tier or non-local model name to the configured Ollama model.

        In local mode, scheme nodes may reference cloud model names like
        ``gemini-2.5-pro``. These need to be mapped to the local Ollama model.
        This flattens the scheme's model tiering; a WARNING is logged per
        distinct mapping so the operator can see that their default scheme's
        tier differentiation has collapsed.
        """
        if tier in (ModelTier.COMPLEX, ModelTier.LIGHT, ModelTier.REASONING):
            self._warn_tier_flatten(tier)
            return self._default_model
        # If the model name doesn't look like a local Ollama model, remap it
        if tier.startswith(("gemini", "claude", "gpt")):
            self._warn_tier_flatten(tier)
            return self._default_model
        return tier

    def _warn_tier_flatten(self, tier: str) -> None:
        """Emit a one-shot warning when a tier/cloud-model name is flattened to the default."""
        if tier in self._warned_tiers:
            return
        self._warned_tiers.add(tier)
        # Normalize a tier/model-name to a hint key. ``tier`` may be a plain
        # string (a cloud model name) or a ModelTier value (string enum), so
        # we look up the enum member by value rather than using .name.
        hint_key: str | None = None
        try:
            hint_key = ModelTier(tier).name
        except ValueError:
            hint_key = None
        hint = self._TIER_HINTS.get(hint_key) if hint_key else None
        logger.warning(
            "[ollama] Flattening tier/model '%s' -> '%s'. "
            "Your scheme's model tiering is collapsed to a single local model — "
            "results will diverge from cloud-model parity. "
            "Recommended local model for this tier: %s",
            tier,
            self._default_model,
            hint or "see docs/schemes.md for recommended local models",
        )

    def supported_models(self) -> list[str]:
        """Return the configured default model as the supported model list."""
        return [self._default_model]

    async def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using a 4-chars-per-token heuristic."""
        return len(text) // 4

    async def generate(
        self,
        messages: list[Message],
        model: str,
        tools: list[ToolDefinition] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 8192,
        system_prompt: str | None = None,
    ) -> LLMResponse:
        """Send a chat completion request to the Ollama API."""
        # C3: up-front tool-calling capability probe. The very first call
        # that passes any tools triggers a lightweight canary request to
        # verify the model can emit tool_calls. A failing probe raises a
        # clear RuntimeError rather than letting the real operative loop
        # silently fall back to text-only output.
        if tools and self._tool_probe_state is None:
            if self._skip_probe:
                self._tool_probe_state = "skipped"
            else:
                await self._probe_tool_calling(model)

        ollama_messages: list[dict[str, Any]] = []
        if system_prompt:
            ollama_messages.append({"role": "system", "content": system_prompt})
        for msg in messages:
            ollama_messages.append({"role": msg.role.value, "content": msg.content})
        payload: dict[str, Any] = {
            "model": model,
            "messages": ollama_messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if tools:
            payload["tools"] = [self._convert_tool(t) for t in tools]
        response = await self._client.post("/api/chat", json=payload)
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", "")
        tool_calls = self._parse_tool_calls(data.get("message", {}))
        prompt_tokens = data.get("prompt_eval_count", 0)
        completion_tokens = data.get("eval_count", 0)

        # If the caller requested tools but the model returned nothing (no tool
        # calls AND no content), surface this as a visible warning — often a
        # signal that the selected Ollama model lacks tool-calling support or
        # its chat template does not emit function calls.
        if tools and not tool_calls and not content.strip():
            logger.warning(
                "[ollama] Model '%s' returned an empty response to a tool-use prompt. "
                "This usually means the model does not support native tool calling. "
                "Raw response keys=%s. Consider switching to qwen2.5-coder:7b, "
                "llama3.3, or another tool-calling-capable model.",
                model,
                sorted(data.keys()),
            )

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            usage=TokenUsage(
                input_tokens=prompt_tokens,
                output_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
                estimated_cost_usd=0.0,
            ),
            model=model,
            finish_reason="tool_use" if tool_calls else "stop",
        )

    async def _probe_tool_calling(self, model: str) -> None:
        """Issue a canary request with a trivial tool to verify tool-calling support.

        Sets ``self._tool_probe_state`` to ``"ok"`` on success or raises a
        ``RuntimeError`` with a clear upgrade hint on failure. Called at
        most once per provider instance, on the first ``generate`` call
        that passes any tools.
        """
        canary_payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Call the probe tool with no arguments."}],
            "stream": False,
            "options": {"temperature": 0.0, "num_predict": 64},
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "probe",
                        "description": "Canary tool for capability detection — call with no arguments.",
                        "parameters": {"type": "object", "properties": {}, "required": []},
                    },
                }
            ],
        }
        try:
            response = await self._client.post("/api/chat", json=canary_payload)
            response.raise_for_status()
            data = response.json()
        except Exception as exc:  # pragma: no cover — network failure wrapped below
            self._tool_probe_state = "failed"
            raise RuntimeError(
                f"Ollama capability probe failed for model '{model}': {exc}. "
                "Verify that an Ollama server is running at the configured base URL "
                "(HENCHMEN_LLM_OLLAMA_BASE_URL). Set HENCHMEN_LLM_OLLAMA_SKIP_PROBE=1 "
                "to bypass the probe if you're running with mocked providers."
            ) from exc

        message = data.get("message", {}) or {}
        if not message.get("tool_calls"):
            self._tool_probe_state = "failed"
            raise RuntimeError(
                f"Ollama model '{model}' does not support native tool calling — "
                "the capability probe returned no tool_calls. Switch to a "
                "tool-calling-capable model such as qwen2.5-coder:7b, "
                "deepseek-r1:8b, or llama3.3. You can override the default model "
                "via HENCHMEN_LLM_OLLAMA_MODEL. If you're intentionally running "
                "without real tool calling (e.g. in tests), set "
                "HENCHMEN_LLM_OLLAMA_SKIP_PROBE=1 to bypass this check."
            )
        self._tool_probe_state = "ok"

    @staticmethod
    def _convert_tool(tool: ToolDefinition) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for p in tool.parameters:
            properties[p.name] = {"type": p.type, "description": p.description}
            if p.required:
                required.append(p.name)
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        }

    @staticmethod
    def _parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
        calls = message.get("tool_calls", [])
        result = []
        for i, call in enumerate(calls):
            fn = call.get("function", {})
            result.append(ToolCall(id=f"call_{i}", name=fn.get("name", ""), arguments=fn.get("arguments", {})))
        return result
