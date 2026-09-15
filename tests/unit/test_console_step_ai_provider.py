"""Tests for the Console's AI provider step.

Model ids are never hardcoded (CLAUDE.md, ruling M-5/R12): every model id used
here is derived from ``Settings.model_fields`` through ``TIER_FIELDS``, the
same source the step itself reads its recommendations from.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pytest

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus
from henchmen.config.settings import Settings
from henchmen.console.state import SetupStep
from henchmen.console.steps.ai_provider import estimate_task_cost, recommended_ceiling_usd, recommended_models
from henchmen.mastermind.scheme_executor.executor import estimate_feature_task_cost, estimate_node_dispatch_cost
from henchmen.models.llm import ModelTier
from henchmen.models.scheme import NodeType
from henchmen.providers.tiers import TIER_FIELDS
from henchmen.schemes.feature_standard import FEATURE_STANDARD
from tests.unit.console_harness import ConsoleHarness, make_harness

BASE = "/console/api/steps/ai_provider"
KEY = "sk-ant-api03-secret-value-0000"


def _tier_key(tier: ModelTier) -> str:
    return tier.name.lower()


def _default_models(provider: str) -> dict[str, str]:
    return {_tier_key(tier): str(Settings.model_fields[field].default) for tier, field in TIER_FIELDS[provider].items()}


ANTHROPIC_DEFAULT_MODELS = _default_models("anthropic")
OPENAI_DEFAULT_MODELS = _default_models("openai")
VERTEX_DEFAULT_MODELS = _default_models("gcp")
ANTHROPIC_MODELS = sorted(ANTHROPIC_DEFAULT_MODELS.values())
DEFAULT_CEILING_USD = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)


@pytest.fixture
def harness(tmp_path: Path) -> ConsoleHarness:
    return make_harness(tmp_path)


def _fake_anthropic(monkeypatch: pytest.MonkeyPatch, *, ok: bool = True, models: list[str] | None = None) -> list[str]:
    seen: list[str] = []

    def check(api_key: str, *, timeout: float = checks.DEFAULT_TIMEOUT) -> CheckResult:
        seen.append(api_key)
        if ok:
            return CheckResult("Anthropic API key", CheckStatus.OK, f"valid ({len(ANTHROPIC_MODELS)} models available)")
        return CheckResult(
            "Anthropic API key",
            CheckStatus.FAIL,
            "rejected: invalid x-api-key",
            hint="Check the key at console.anthropic.com",
        )

    monkeypatch.setattr(checks, "check_anthropic_key", check)
    monkeypatch.setattr(
        checks,
        "list_anthropic_models",
        lambda api_key, *, timeout=checks.DEFAULT_TIMEOUT: list(ANTHROPIC_MODELS if models is None else models),
    )
    return seen


def _expected_cost(provider: str, models: dict[str, str]) -> float:
    overrides: dict[str, Any] = {"_env_file": None, "provider": "local", "llm_provider": provider}
    for tier, field in TIER_FIELDS[provider].items():
        overrides[field] = models[_tier_key(tier)]
    settings = Settings(**overrides)
    return round(estimate_feature_task_cost(settings), 2)


# Comfortably above the Anthropic default-tier estimate (never a literal guess at that
# number, which would drift out of sync with pricing/budget changes exactly as a
# previous version of this constant did when the estimate started pricing the whole
# scheme instead of one node -- ruling C2a).
SAFE_CEILING_USD = round(_expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS) * 2, 2)


def _save_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "provider": "anthropic",
        "api_key": KEY,
        "models": dict(ANTHROPIC_DEFAULT_MODELS),
        "task_cost_ceiling_usd": SAFE_CEILING_USD,
    }
    body.update(overrides)
    return body


def test_requires_a_session(tmp_path: Path) -> None:
    assert make_harness(tmp_path, signed_in=False).get(BASE).status_code == 401


def test_current_before_anything_is_saved(harness: ConsoleHarness) -> None:
    body = harness.get(BASE).json()
    assert body["ok"] is True
    assert body["step"] == "ai_provider"
    assert [p["id"] for p in body["details"]["providers"]] == ["anthropic", "openai", "gcp", "aws", "local"]
    assert body["details"]["providers"][0]["recommended"] is True
    assert body["details"]["provider"] == ""
    assert body["details"]["task_cost_ceiling_usd"] == DEFAULT_CEILING_USD


def test_validate_lists_models_and_recommends_tier_defaults(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _fake_anthropic(monkeypatch)
    response = harness.post(f"{BASE}/validate", {"provider": "anthropic", "api_key": KEY})
    body = response.json()
    expected_cost = _expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    expected_ceiling = recommended_ceiling_usd(expected_cost)
    assert response.status_code == 200
    assert body["ok"] is True
    assert body["details"]["models"] == ANTHROPIC_MODELS
    assert body["details"]["recommended"] == ANTHROPIC_DEFAULT_MODELS
    assert body["details"]["estimated_cost_per_task_usd"] == expected_cost
    assert body["details"]["recommended_task_cost_ceiling_usd"] == expected_ceiling
    assert expected_ceiling >= expected_cost
    assert f"{expected_cost:.2f}" in body["details"]["spending_limit_explanation"]
    assert seen == [KEY]
    assert KEY not in response.text
    assert not harness.config_store.config_file.exists()


def test_validate_reports_a_rejected_key(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch, ok=False)
    body = harness.post(f"{BASE}/validate", {"provider": "anthropic", "api_key": "sk-wrong"}).json()
    assert body == {
        "ok": False,
        "step": "ai_provider",
        "problems": [
            {
                "field": "api_key",
                "message": "Anthropic API key: rejected: invalid x-api-key",
                "action": "Check the key at console.anthropic.com",
            }
        ],
    }


def test_validate_without_a_key_asks_for_one(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_anthropic(monkeypatch)
    body = harness.post(f"{BASE}/validate", {"provider": "anthropic"}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "api_key"
    assert "console.anthropic.com" in body["problems"][0]["action"]
    assert seen == []


def test_validate_fails_closed_when_the_provider_lists_no_models(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_anthropic(monkeypatch, models=[])
    body = harness.post(f"{BASE}/validate", {"provider": "anthropic", "api_key": KEY}).json()
    assert body["ok"] is False
    assert body["problems"] == [
        {
            "field": "models.complex",
            "message": "Henchmen could not list the models this account can use.",
            "action": "Choose Check again.",
        }
    ]


def test_save_writes_config_and_completes_the_step(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch)
    response = harness.post(BASE, _save_body())
    body = response.json()
    expected_cost = _expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    assert body["ok"] is True
    assert body["details"]["credential"] == "configured"
    assert body["details"]["models"] == ANTHROPIC_DEFAULT_MODELS
    assert body["details"]["estimated_cost_per_task_usd"] == expected_cost
    assert body["details"]["task_cost_ceiling_usd"] == SAFE_CEILING_USD
    assert body["details"]["ceiling_below_estimate"] is False
    assert "warning" not in body["details"]
    assert KEY not in response.text

    store = harness.config_store
    assert store.get("HENCHMEN_PROVIDER") == "local"
    assert store.get("HENCHMEN_LLM_PROVIDER") == "anthropic"
    assert store.get("HENCHMEN_ANTHROPIC_API_KEY") == KEY
    assert store.get("HENCHMEN_ANTHROPIC_MODEL_COMPLEX") == ANTHROPIC_DEFAULT_MODELS["complex"]
    assert store.get("HENCHMEN_ANTHROPIC_MODEL_LIGHT") == ANTHROPIC_DEFAULT_MODELS["light"]
    assert store.get("HENCHMEN_ANTHROPIC_MODEL_REASONING") == ANTHROPIC_DEFAULT_MODELS["reasoning"]
    assert store.get("HENCHMEN_LLM_CHAT_MODEL") == ANTHROPIC_DEFAULT_MODELS["light"]
    assert store.get("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD") == f"{SAFE_CEILING_USD:g}"
    assert SetupStep.AI_PROVIDER in harness.setup_store.load().completed_steps


def test_save_without_a_ceiling_writes_the_recommendation(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_anthropic(monkeypatch)
    body = harness.post(BASE, _save_body(task_cost_ceiling_usd=None)).json()
    expected_cost = _expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    expected_ceiling = recommended_ceiling_usd(expected_cost)
    assert body["ok"] is True
    assert body["details"]["task_cost_ceiling_usd"] == expected_ceiling
    assert body["details"]["ceiling_below_estimate"] is False
    assert harness.config_store.get("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD") == f"{expected_ceiling:g}"


def test_save_below_the_estimate_still_saves_with_a_warning(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_anthropic(monkeypatch)
    body = harness.post(BASE, _save_body(task_cost_ceiling_usd=0.01)).json()
    assert body["ok"] is True
    assert body["details"]["task_cost_ceiling_usd"] == 0.01
    assert body["details"]["ceiling_below_estimate"] is True
    assert "warning" in body["details"]
    assert harness.config_store.get("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD") == "0.01"


def test_save_keeps_an_existing_provider_setting(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch)
    harness.config_store.update({"HENCHMEN_PROVIDER": "gcp", "HENCHMEN_GCP_PROJECT_ID": "p"}, section="Provider")
    assert harness.post(BASE, _save_body()).json()["ok"] is True
    assert harness.config_store.get("HENCHMEN_PROVIDER") == "gcp"


def test_save_switching_provider_unsets_the_previous_api_key(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_anthropic(monkeypatch)
    assert harness.post(BASE, _save_body()).json()["ok"] is True
    assert harness.config_store.get("HENCHMEN_ANTHROPIC_API_KEY") == KEY

    monkeypatch.setattr(
        checks,
        "check_openai_key",
        lambda api_key, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult("OpenAI API key", CheckStatus.OK, "valid"),
    )
    monkeypatch.setattr(
        checks,
        "list_openai_models",
        lambda api_key, *, timeout=checks.DEFAULT_TIMEOUT: list(OPENAI_DEFAULT_MODELS.values()),
    )
    other_key = "sk-openai-secret-0000000000000000"
    body = harness.post(
        BASE,
        {
            "provider": "openai",
            "api_key": other_key,
            "models": dict(OPENAI_DEFAULT_MODELS),
            "task_cost_ceiling_usd": 12.0,
        },
    ).json()
    assert body["ok"] is True
    assert harness.config_store.get("HENCHMEN_OPENAI_API_KEY") == other_key
    assert harness.config_store.get("HENCHMEN_ANTHROPIC_API_KEY") == ""


def test_save_reports_a_config_store_rejection_instead_of_500(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checks,
        "check_vertex",
        lambda project_id, region, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult(
            "Vertex AI credentials", CheckStatus.OK, "ADC present"
        ),
    )
    response = harness.post(
        BASE,
        {
            "provider": "gcp",
            "gcp_project_id": "acme-prod\nHENCHMEN_EVIL=1",
            "models": dict(VERTEX_DEFAULT_MODELS),
            "task_cost_ceiling_usd": 12.0,
        },
    )
    body = response.json()
    assert response.status_code == 200
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "gcp_project_id"
    assert not harness.config_store.config_file.exists()


def test_failed_save_writes_nothing(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch, ok=False)
    assert harness.post(BASE, _save_body()).json()["ok"] is False
    assert not harness.config_store.config_file.exists()
    assert SetupStep.AI_PROVIDER not in harness.setup_store.load().completed_steps


def test_save_reuses_the_stored_key(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _fake_anthropic(monkeypatch)
    harness.config_store.update({"HENCHMEN_ANTHROPIC_API_KEY": KEY}, section="LLM")
    assert harness.post(BASE, _save_body(api_key="")).json()["ok"] is True
    assert seen == [KEY]


def test_save_rejects_a_model_the_key_cannot_reach(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch)
    unreachable = {**ANTHROPIC_DEFAULT_MODELS, "complex": ANTHROPIC_DEFAULT_MODELS["complex"] + "-imaginary"}
    body = harness.post(BASE, _save_body(models=unreachable)).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "models.complex"
    assert not harness.config_store.config_file.exists()


def test_save_fails_closed_when_the_provider_lists_no_models(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_anthropic(monkeypatch, models=[])
    body = harness.post(BASE, _save_body()).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "models.complex"
    assert not harness.config_store.config_file.exists()


def test_ceiling_must_be_positive(harness: ConsoleHarness) -> None:
    assert harness.post(BASE, _save_body(task_cost_ceiling_usd=0)).status_code == 422


def test_current_masks_the_saved_key(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_anthropic(monkeypatch)
    harness.post(BASE, _save_body())
    response = harness.get(BASE)
    details = response.json()["details"]
    assert details["provider"] == "anthropic"
    assert details["credential"] == "configured"
    assert details["models"] == ANTHROPIC_DEFAULT_MODELS
    assert details["task_cost_ceiling_usd"] == SAFE_CEILING_USD
    assert KEY not in response.text


def test_current_normalizes_a_saved_provider_alias(harness: ConsoleHarness) -> None:
    harness.config_store.update({"HENCHMEN_LLM_PROVIDER": "vertex"}, section="LLM")
    body = harness.get(BASE).json()
    assert body["details"]["provider"] == "gcp"


def test_current_recommends_a_ceiling_when_a_provider_was_saved_without_one(harness: ConsoleHarness) -> None:
    models = {
        f"HENCHMEN_{field.upper()}": ANTHROPIC_DEFAULT_MODELS[_tier_key(tier)]
        for tier, field in TIER_FIELDS["anthropic"].items()
    }
    harness.config_store.update({"HENCHMEN_LLM_PROVIDER": "anthropic", **models}, section="LLM")
    expected_cost = _expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    expected_ceiling = recommended_ceiling_usd(expected_cost)
    body = harness.get(BASE).json()
    assert body["details"]["task_cost_ceiling_usd"] == expected_ceiling


def test_local_models_write_ollama_settings(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    local_model = str(Settings.model_fields["llm_ollama_model"].default)
    monkeypatch.setattr(
        checks,
        "check_ollama",
        lambda base_url, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult("Ollama", CheckStatus.OK, "reachable"),
    )
    monkeypatch.setattr(checks, "list_ollama_models", lambda base_url, *, timeout=checks.DEFAULT_TIMEOUT: [local_model])
    models = {"complex": local_model, "light": local_model, "reasoning": local_model}
    body = harness.post(
        BASE,
        {
            "provider": "local",
            "ollama_base_url": "http://host.docker.internal:11434",
            "models": models,
            "task_cost_ceiling_usd": 1,
        },
    ).json()
    assert body["ok"] is True
    assert body["details"]["estimated_cost_per_task_usd"] == 0.0
    assert body["details"]["ceiling_below_estimate"] is False
    assert harness.config_store.get("HENCHMEN_LLM_OLLAMA_BASE_URL") == "http://host.docker.internal:11434"
    assert harness.config_store.get("HENCHMEN_LLM_OLLAMA_MODEL") == local_model
    assert harness.config_store.get("HENCHMEN_LLM_OLLAMA_MODEL_REASONING") == local_model


def test_unreachable_ollama_explains_host_docker_internal(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        checks,
        "check_ollama",
        lambda base_url, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult(
            "Ollama", CheckStatus.FAIL, f"cannot reach {base_url}", hint="Start it with: ollama serve"
        ),
    )
    body = harness.post(f"{BASE}/validate", {"provider": "local", "ollama_base_url": "http://localhost:11434"}).json()
    assert body["ok"] is False
    assert body["problems"][0]["field"] == "ollama_base_url"
    assert "host.docker.internal" in body["problems"][0]["action"]


def test_vertex_offers_the_gemini_catalog(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checks,
        "check_vertex",
        lambda project_id, region, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult(
            "Vertex AI credentials", CheckStatus.OK, "ADC present"
        ),
    )
    body = harness.post(f"{BASE}/validate", {"provider": "gcp", "gcp_project_id": "acme-prod"}).json()
    assert body["ok"] is True
    assert body["details"]["models"] == list(checks.VERTEX_MODELS)
    assert body["details"]["recommended"] == VERTEX_DEFAULT_MODELS


def test_estimate_task_cost_uses_the_price_table() -> None:
    cost = estimate_task_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    assert cost == _expected_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    assert cost > 0


def test_recommended_models_reads_settings_defaults() -> None:
    assert recommended_models("anthropic", ANTHROPIC_MODELS) == ANTHROPIC_DEFAULT_MODELS
    assert recommended_models("openai", []) == OPENAI_DEFAULT_MODELS


def test_recommended_ceiling_usd_covers_the_estimate_with_headroom() -> None:
    assert recommended_ceiling_usd(0.0) == DEFAULT_CEILING_USD
    big_estimate = DEFAULT_CEILING_USD * 10
    assert recommended_ceiling_usd(big_estimate) == float(math.ceil(big_estimate * 1.5))


@pytest.mark.parametrize("provider", ["anthropic", "openai", "gcp"])
def test_default_ceiling_covers_the_feature_task_estimate(provider: str) -> None:
    """Simulate the executor's pre-dispatch cost gate (ruling C2a).

    Rather than merely re-checking the arithmetic identity that
    ``recommended_ceiling_usd`` always returns at least its input (true by
    construction, and already covered by
    ``test_recommended_ceiling_usd_covers_the_estimate_with_headroom``), this
    dispatches every agentic node of the feature scheme in turn -- exactly as
    the executor's own cumulative-cost gate would -- and asserts the running
    total never exceeds the recommended ceiling, for each provider's default
    tiers.
    """
    models = _default_models(provider)
    overrides: dict[str, Any] = {"_env_file": None, "provider": "local", "llm_provider": provider}
    for tier, field in TIER_FIELDS[provider].items():
        overrides[field] = models[_tier_key(tier)]
    settings = Settings(**overrides)
    agentic_nodes = [node for node in FEATURE_STANDARD.nodes if node.node_type == NodeType.AGENTIC]
    assert len(agentic_nodes) >= 2  # implement_feature and fix_tests, at minimum

    ceiling = recommended_ceiling_usd(estimate_task_cost(provider, models))
    cumulative = 0.0
    for node in agentic_nodes:
        cumulative += estimate_node_dispatch_cost(settings, node)
        assert cumulative <= ceiling


def test_recommended_models_falls_back_to_an_available_model() -> None:
    """When the account's listing lacks the Settings default, fall back to a model it does offer."""
    limited = [ANTHROPIC_DEFAULT_MODELS["light"], "an-unrelated-model"]
    recs = recommended_models("anthropic", limited)
    assert recs["light"] == ANTHROPIC_DEFAULT_MODELS["light"]
    assert recs["complex"] == limited[0]
    assert recs["reasoning"] == limited[0]


def test_problem_from_check_redacts_a_key_echoed_back_by_the_provider(
    harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check that (bug or not) echoes the submitted key back must never leak it to the browser."""
    monkeypatch.setattr(
        checks,
        "check_anthropic_key",
        lambda api_key, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult(
            "Anthropic API key", CheckStatus.FAIL, f"rejected: bad key {api_key}", hint=f"Retry with {api_key}"
        ),
    )
    response = harness.post(f"{BASE}/validate", {"provider": "anthropic", "api_key": KEY})
    assert KEY not in response.text


def test_problem_from_check_redacts_url_userinfo(harness: ConsoleHarness, monkeypatch: pytest.MonkeyPatch) -> None:
    """A leaked basic-auth Ollama URL in a check's error text must never reach the browser."""
    leaking_url = "http://admin:hunter2@localhost:11434"
    monkeypatch.setattr(
        checks,
        "check_ollama",
        lambda base_url, *, timeout=checks.DEFAULT_TIMEOUT: CheckResult(
            "Ollama", CheckStatus.FAIL, f"cannot reach {leaking_url}", hint=f"Start it with: {leaking_url}"
        ),
    )
    response = harness.post(f"{BASE}/validate", {"provider": "local", "ollama_base_url": leaking_url})
    assert "admin:hunter2" not in response.text


def test_estimate_task_cost_applies_saved_token_budgets(harness: ConsoleHarness) -> None:
    """A previously saved HENCHMEN_OPERATIVE_MAX_* budget changes the estimate to match runtime."""
    unbounded = estimate_task_cost("anthropic", ANTHROPIC_DEFAULT_MODELS)
    harness.config_store.update(
        {
            "HENCHMEN_OPERATIVE_MAX_SYSTEM_TOKENS": "100",
            "HENCHMEN_OPERATIVE_MAX_MESSAGE_TOKENS": "100",
            "HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS": "50",
        },
        section="Operative",
    )
    bounded = estimate_task_cost("anthropic", ANTHROPIC_DEFAULT_MODELS, harness.config_store)
    assert bounded < unbounded
