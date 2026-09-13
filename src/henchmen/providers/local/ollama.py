"""Ollama implementation of LLMProvider for local development."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import httpx

from henchmen.models.llm import LLMResponse, Message, MessageRole, ModelTier, TokenUsage, ToolCall, ToolDefinition
from henchmen.providers.llm_common import json_schema, normalize_finish_reason, resolve_provider_model
from henchmen.providers.tiers import TIER_FIELDS, is_tier_name

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

PROVIDER_NAME = "local"


class OllamaProvider:
    """LLMProvider backed by a local Ollama server.

    .. note::
       BYO-LLM via Ollama is experimental. Each tier can name its own local
       model (``HENCHMEN_LLM_OLLAMA_MODEL_COMPLEX`` / ``_LIGHT`` / ``_REASONING``);
       a tier left unset falls back to ``HENCHMEN_LLM_OLLAMA_MODEL`` and logs a
       one-shot warning, because running every node on one model flattens the
       scheme's tiering and diverges from cloud-model parity.

       For best results, use an Ollama model with native tool-calling support
       (e.g. ``qwen2.5-coder:7b`` or ``llama3.3``). Models like ``llama3.2``
       have known weaknesses around function calling under Ollama.
    """

    # Recommended local model per tier, surfaced in the fallback warning.
    _TIER_HINTS: dict[str, str] = {
        "COMPLEX": "qwen2.5-coder:7b (tool-calling capable, strong for code)",
        "LIGHT": "qwen2.5:3b (smaller, faster for planning/analysis)",
        "REASONING": "deepseek-r1:8b (reasoning-heavy tasks like fix_tests)",
    }

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_url = getattr(settings, "llm_ollama_base_url", "http://localhost:11434")
        self._default_model = getattr(settings, "llm_ollama_model", "llama3.2")
        self._skip_probe = bool(getattr(settings, "llm_ollama_skip_probe", False))
        self._client = httpx.AsyncClient(base_url=self._base_url, timeout=300.0)
        # Which tiers have their own model configured; the rest fall back to
        # llm_ollama_model and warn once.
        self._explicit_tiers: dict[str, bool] = {
            tier.value: bool(str(getattr(settings, field, "") or ""))
            for tier, field in TIER_FIELDS[PROVIDER_NAME].items()
        }
        self._warned_tiers: set[str] = set()
        # C3 capability probe state: None → not probed, "ok" → probed OK,
        # "skipped" → short-circuited via llm_ollama_skip_probe, "failed"
        # → the probe detected the model cannot emit tool calls.
        self._tool_probe_state: str | None = None

    def resolve_tier(self, tier: str) -> str:
        """Resolve a tier to its configured local model, or flatten a cloud model name.

        A tier with its own ``HENCHMEN_LLM_OLLAMA_MODEL_<TIER>`` resolves
        silently. A tier without one falls back to ``llm_ollama_model`` and
        warns once, as does any non-local model name a scheme still references.
        """
        if is_tier_name(tier):
            resolved = resolve_provider_model(self._settings, tier, PROVIDER_NAME)
            if not self._explicit_tiers.get(tier, False):
                self._warn_tier_flatten(tier, resolved)
            return resolved
        # If the model name doesn't look like a local Ollama model, remap it
        if tier.startswith(("gemini", "claude", "gpt")):
            self._warn_tier_flatten(tier, self._default_model)
            return self._default_model
        return tier

    def _warn_tier_flatten(self, tier: str, resolved: str) -> None:
        """Emit a one-shot warning when a tier/cloud-model name is flattened to the default."""
        if tier in self._warned_tiers:
            return
        self._warned_tiers.add(tier)
        # ``tier`` may be a plain string (a cloud model name) or a ModelTier
        # value, so look up the enum member by value rather than using .name.
        hint_key: str | None
        try:
            hint_key = ModelTier(tier).name
        except ValueError:
            hint_key = None
        hint = self._TIER_HINTS.get(hint_key) if hint_key else None
        setting = TIER_FIELDS[PROVIDER_NAME].get(ModelTier(tier)) if hint_key else None
        logger.warning(
            "[ollama] Flattening tier/model '%s' -> '%s'. "
            "Your scheme's model tiering is collapsed to a single local model — "
            "results will diverge from cloud-model parity. Set HENCHMEN_%s to give this tier its own model. "
            "Recommended local model for this tier: %s",
            tier,
            resolved,
            (setting or "LLM_OLLAMA_MODEL").upper(),
            hint or "see docs/schemes.md for recommended local models",
        )

    def supported_models(self) -> list[str]:
        """Return the distinct local models configured across the tiers."""
        seen: set[str] = set()
        out: list[str] = []
        tier_configured = (str(getattr(self._settings, f, "") or "") for f in TIER_FIELDS[PROVIDER_NAME].values())
        for name in (self._default_model, *tier_configured):
            if name and name not in seen:
                seen.add(name)
                out.append(name)
        return out

    async def count_tokens(self, text: str, model: str) -> int:
        """Approximate token count using a 4-chars-per-token heuristic (model-independent)."""
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
        # Always resolve tier/cloud model names to a local Ollama model.
        model = self.resolve_tier(model)

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
        ollama_messages.extend(self._build_messages(messages))
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
                # Local inference has no per-token price.
                estimated_cost_usd=0.0,
            ),
            model=model,
            finish_reason=normalize_finish_reason(data.get("done_reason"), has_tool_calls=bool(tool_calls)),
        )

    @staticmethod
    def _build_messages(messages: list[Message]) -> list[dict[str, Any]]:
        """Convert henchmen messages to the Ollama chat payload.

        Assistant turns replay their ``tool_calls`` so the chat template renders
        each tool result next to the call it answers.
        """
        call_names: dict[str, str] = {}
        for msg in messages:
            for tc in msg.tool_calls or []:
                call_names[tc.id] = tc.name

        result: list[dict[str, Any]] = []
        for msg in messages:
            entry: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
            if msg.role == MessageRole.TOOL:
                name = call_names.get(msg.tool_call_id or "")
                if name:
                    entry["tool_name"] = name
            elif msg.role == MessageRole.ASSISTANT and msg.tool_calls:
                entry["tool_calls"] = [
                    {"function": {"name": tc.name, "arguments": tc.arguments}} for tc in msg.tool_calls
                ]
            result.append(entry)
        return result

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
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": json_schema(tool),
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
