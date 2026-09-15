"""Console step 1: choose, verify and price the AI provider (spec Section 4, Section 5.4).

Credentials are checked with the same ``cli.checks`` functions ``henchmen
init`` and ``henchmen doctor`` use, so the Console and the CLI accept exactly
the same keys. The blocking SDK calls run in a worker thread. The recommended
model per tier is the ``Settings`` default for that tier (or, when the
account cannot reach it, the first model the account's own listing offers),
and the per-task cost estimate prices a full feature task -- including one
round of test fixes -- through
``mastermind.scheme_executor.executor.estimate_feature_task_cost`` --
which itself goes through ``providers.pricing.estimate_cost_for_settings`` --
so the number shown here always agrees with the cost gate that would
otherwise refuse the task (ruling C2a). There is no second price table or
token profile here.

A save re-validates the credential (an earlier ``/validate`` is never
trusted), writes through ``ConfigStore`` and only then records the step
complete.
"""

from __future__ import annotations

import logging
import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus
from henchmen.config.settings import Settings
from henchmen.console.check_problems import problem_from_check
from henchmen.console.config_store import CONFIGURED, ConfigStore, ConfigStoreError
from henchmen.console.deps import get_config_store
from henchmen.console.state import SetupStateStore, SetupStep
from henchmen.console.steps import (
    StepFailure,
    StepProblem,
    StepSuccess,
    get_setup_store,
    step_failed,
    step_succeeded,
)
from henchmen.mastermind.scheme_executor.executor import estimate_feature_task_cost
from henchmen.models.llm import ModelTier
from henchmen.providers.tiers import TIER_FIELDS, normalize_llm_provider

logger = logging.getLogger(__name__)

router = APIRouter()
STEP = SetupStep.AI_PROVIDER
CONFIG_SECTION = "LLM"

ProviderName = Literal["anthropic", "openai", "gcp", "aws", "local"]

PROVIDER_OPTIONS: tuple[dict[str, Any], ...] = (
    {
        "id": "anthropic",
        "label": "Anthropic",
        "recommended": True,
        "credential": "api_key",
        "key_url": "https://console.anthropic.com/settings/keys",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "recommended": False,
        "credential": "api_key",
        "key_url": "https://platform.openai.com/api-keys",
    },
    {
        "id": "gcp",
        "label": "Google Vertex AI",
        "recommended": False,
        "credential": "gcp_project_id",
        "key_url": "https://console.cloud.google.com/vertex-ai",
    },
    {
        "id": "aws",
        "label": "AWS Bedrock",
        "recommended": False,
        "credential": "aws_region",
        "key_url": "https://console.aws.amazon.com/bedrock/home",
    },
    {
        "id": "local",
        "label": "Local models (advanced)",
        "recommended": False,
        "credential": "ollama_base_url",
        "key_url": "https://ollama.com/download",
    },
)

_KEY_URLS: dict[str, str] = {str(option["id"]): str(option["key_url"]) for option in PROVIDER_OPTIONS}
_CREDENTIAL_FIELDS: dict[str, str] = {str(option["id"]): str(option["credential"]) for option in PROVIDER_OPTIONS}
_API_KEY_SETTINGS: dict[str, str] = {
    "anthropic": "HENCHMEN_ANTHROPIC_API_KEY",
    "openai": "HENCHMEN_OPENAI_API_KEY",
}
_CEILING_KEY = "HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD"
_OLLAMA_IN_CONTAINER_ACTION = (
    "If Ollama runs on this computer, use http://host.docker.internal:11434 — inside Henchmen, "
    "localhost means Henchmen itself. Start Ollama with: ollama serve"
)
_NO_MODELS_PROBLEM = StepProblem(
    field="models.complex",
    message="Henchmen could not list the models this account can use.",
    action="Choose Check again.",
)
_COULD_NOT_ESTIMATE_PROBLEM = StepProblem(
    message="Henchmen could not estimate the cost of a task with these models.",
    action="Try again, or choose Check again.",
)

# Token-budget Settings fields the estimate must reflect if they were already
# customized (ruling: "estimate_task_cost ... applies them, so it matches
# runtime"), rather than always assuming the Settings hard-coded defaults.
_TOKEN_BUDGET_FIELDS: tuple[str, ...] = (
    "operative_max_system_tokens",
    "operative_max_message_tokens",
    "operative_max_output_tokens",
)

# Reverse mapping from a settings env key back to the request field it came
# from, used only to attribute a ConfigStoreError to a field instead of
# surfacing a bare 500 (per-tier model keys are matched separately, since
# they depend on the chosen provider).
_KEY_TO_FIELD: dict[str, str] = {
    "HENCHMEN_LLM_OLLAMA_BASE_URL": "ollama_base_url",
    "HENCHMEN_GCP_PROJECT_ID": "gcp_project_id",
    "HENCHMEN_GCP_REGION": "gcp_region",
    "HENCHMEN_AWS_REGION": "aws_region",
    "HENCHMEN_ANTHROPIC_API_KEY": "api_key",
    "HENCHMEN_OPENAI_API_KEY": "api_key",
    _CEILING_KEY: "task_cost_ceiling_usd",
}

ConfigDep = Annotated[ConfigStore, Depends(get_config_store)]
SetupDep = Annotated[SetupStateStore, Depends(get_setup_store)]


class AiCredentials(BaseModel):
    """What the user entered to reach a provider. Secrets are write-only."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    provider: ProviderName = Field(..., description="Provider registry name")
    api_key: str = Field(default="", max_length=512, description="Anthropic/OpenAI key; blank reuses the saved one")
    ollama_base_url: str = Field(default="", max_length=512, description="Ollama server URL")
    gcp_project_id: str = Field(default="", max_length=128, description="GCP project for Vertex AI")
    gcp_region: str = Field(default="us-central1", max_length=64, description="GCP region for Vertex AI")
    aws_region: str = Field(default="us-east-1", max_length=64, description="AWS region for Bedrock")


class TierModels(BaseModel):
    """The concrete model chosen for each tier."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    complex: str = Field(..., min_length=1, max_length=200, description="COMPLEX tier model")
    light: str = Field(..., min_length=1, max_length=200, description="LIGHT tier model")
    reasoning: str = Field(..., min_length=1, max_length=200, description="REASONING tier model")

    def as_dict(self) -> dict[str, str]:
        return {"complex": self.complex, "light": self.light, "reasoning": self.reasoning}


class AiProviderSave(AiCredentials):
    """A full AI provider configuration to save.

    ``task_cost_ceiling_usd`` is optional: when omitted, the save writes the
    recommended ceiling for the chosen models (ruling C2a). When given below
    the estimate it is still saved as-is -- the user's explicit choice always
    wins -- and the response carries a warning.
    """

    models: TierModels = Field(..., description="Model per tier")
    task_cost_ceiling_usd: float | None = Field(
        default=None, gt=0, le=1000, description="Per-task spending limit in USD; omit to use the recommendation"
    )


def _tier_key(tier: ModelTier) -> str:
    return tier.name.lower()


def _env_key(field_name: str) -> str:
    return f"HENCHMEN_{field_name.upper()}"


def _field_for_key(provider: str, key: str) -> str | None:
    """Map a settings env key back to the request field it came from, for error attribution."""
    if key in _KEY_TO_FIELD:
        return _KEY_TO_FIELD[key]
    for tier, field_name in TIER_FIELDS.get(provider, {}).items():
        if _env_key(field_name) == key:
            return f"models.{_tier_key(tier)}"
    return None


def recommended_models(provider: str, available: list[str]) -> dict[str, str]:
    """The ``Settings`` default model per tier when the account can reach it.

    When ``available`` is non-empty and the default is not in it, falls back
    to the first model the account's own listing offers for that tier --
    never a model the key or server cannot actually use.
    """
    picks: dict[str, str] = {}
    for tier, field_name in TIER_FIELDS[provider].items():
        default = str(Settings.model_fields[field_name].default or "")
        if not default and provider == "local":
            default = str(Settings.model_fields["llm_ollama_model"].default or "")
        if available and default not in available:
            default = available[0]
        picks[_tier_key(tier)] = default
    return picks


def _token_budget_overrides(config: ConfigStore | None) -> dict[str, int]:
    """Any ``HENCHMEN_OPERATIVE_MAX_*`` token budgets already saved, so the estimate matches runtime."""
    if config is None:
        return {}
    overrides: dict[str, int] = {}
    for field_name in _TOKEN_BUDGET_FIELDS:
        raw = config.get(_env_key(field_name)).strip()
        if not raw:
            continue
        try:
            overrides[field_name] = int(raw)
        except ValueError:
            logger.warning("Ignoring non-integer %s in the config file", _env_key(field_name))
    return overrides


def _raw_task_cost(provider: str, models: dict[str, str], config: ConfigStore | None = None) -> float:
    """Unrounded USD cost of a full feature task; see :func:`estimate_task_cost`.

    A ``ceiling_below_estimate`` decision must compare against this, not the
    rounded display figure, since rounding down a cent could hide a limit
    that is actually below what the executor's cost gate would charge.
    """
    overrides: dict[str, Any] = {"_env_file": None, "provider": "local", "llm_provider": provider}
    for tier, field_name in TIER_FIELDS[provider].items():
        overrides[field_name] = models[_tier_key(tier)]
    overrides.update(_token_budget_overrides(config))
    settings = Settings(**overrides)
    return estimate_feature_task_cost(settings)


def estimate_task_cost(provider: str, models: dict[str, str], config: ConfigStore | None = None) -> float:
    """USD cost of a full feature task with ``models``, rounded to cents for display.

    Sums every agentic node of the ``feature_standard`` scheme -- not only
    ``implement_feature`` but also one round of ``fix_tests`` -- so this
    always agrees with what the executor's own pre-dispatch cost gate would
    charge (ruling C2a). When ``config`` is given, any already-saved
    ``HENCHMEN_OPERATIVE_MAX_*`` token budgets are applied instead of the
    ``Settings`` defaults, so the estimate matches the configured runtime.
    """
    return round(_raw_task_cost(provider, models, config), 2)


def recommended_ceiling_usd(estimate: float) -> float:
    """A per-task spending limit that covers ``estimate`` with headroom (ruling C2a)."""
    default = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)
    return float(max(default, math.ceil(estimate * 1.5)))


def _spending_limit_explanation(estimate: float, recommended: float) -> str:
    return (
        f"A feature task, including one round of test fixes, can cost up to about ${estimate:.2f} "
        f"with these models. We suggest a limit of ${recommended:.0f} per task; Henchmen stops a "
        "task before it would go over."
    )


def _price_or_none(
    provider: str, models: dict[str, str], config: ConfigStore | None = None
) -> tuple[float, float] | None:
    """``(raw, rounded)`` cost of a feature task, or ``None`` (logged) when pricing fails."""
    try:
        raw = _raw_task_cost(provider, models, config)
    except Exception:
        logger.exception("Could not estimate the feature-task cost for provider %s", provider)
        return None
    return raw, round(raw, 2)


def _check_and_list(body: AiCredentials, api_key: str) -> tuple[CheckResult, list[str]]:
    """Blocking credential check plus model listing (run in a worker thread)."""
    if body.provider == "anthropic":
        result = checks.check_anthropic_key(api_key)
        return result, (checks.list_anthropic_models(api_key) if result.is_ok else [])
    if body.provider == "openai":
        result = checks.check_openai_key(api_key)
        return result, (checks.filter_openai_models(checks.list_openai_models(api_key)) if result.is_ok else [])
    if body.provider == "local":
        result = checks.check_ollama(body.ollama_base_url)
        return result, (checks.list_ollama_models(body.ollama_base_url) if result.is_ok else [])
    if body.provider == "gcp":
        return checks.check_vertex(body.gcp_project_id, body.gcp_region), list(checks.VERTEX_MODELS)
    result = checks.check_bedrock(body.aws_region)
    return result, (checks.list_bedrock_models(body.aws_region) if result.is_ok else [])


async def _verify(body: AiCredentials, config: ConfigStore) -> tuple[str, list[str], StepFailure | None]:
    """Return ``(api_key, models, None)`` when the credential works, else a failure response.

    An empty model list is never treated as a silent skip of model
    validation (ruling PM-6): once the credential check itself reports OK,
    a still-empty listing for anything but ``gcp`` (whose catalog is a fixed
    tuple, never empty) fails closed rather than accepting any model name.
    """
    api_key = ""
    key_setting = _API_KEY_SETTINGS.get(body.provider)
    if key_setting is not None:
        api_key = body.api_key or config.get(key_setting)
        if not api_key:
            problem = StepProblem(
                field="api_key",
                message="Paste your API key to continue.",
                action=f"Create one at {_KEY_URLS[body.provider]}",
            )
            return "", [], step_failed(STEP, problem)
    if body.provider == "local" and not body.ollama_base_url:
        problem = StepProblem(
            field="ollama_base_url",
            message="Enter the address of your Ollama server.",
            action=_OLLAMA_IN_CONTAINER_ACTION,
        )
        return "", [], step_failed(STEP, problem)

    result, models = await run_in_threadpool(_check_and_list, body, api_key)
    if result.status != CheckStatus.OK:
        problem = problem_from_check(result, field=_CREDENTIAL_FIELDS[body.provider])
        if body.provider == "local":
            problem = problem.model_copy(update={"action": _OLLAMA_IN_CONTAINER_ACTION})
        return "", [], step_failed(STEP, problem)
    if body.provider != "gcp" and not models:
        return "", [], step_failed(STEP, _NO_MODELS_PROBLEM)
    return api_key, models, None


def _saved_models(config: ConfigStore, provider: str) -> dict[str, str]:
    if provider not in TIER_FIELDS:
        return {}
    return {_tier_key(tier): config.get(_env_key(name)) for tier, name in TIER_FIELDS[provider].items()}


def _ceiling(config: ConfigStore, provider: str, models: dict[str, str]) -> float:
    """The saved ceiling, or -- once a provider is saved -- the recommendation for it."""
    saved = config.get(_CEILING_KEY).strip()
    if saved:
        try:
            return float(saved)
        except ValueError:
            logger.warning("Ignoring non-numeric %s in the config file: %r", _CEILING_KEY, saved)
    default = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)
    if provider in TIER_FIELDS and all(models.values()):
        try:
            return recommended_ceiling_usd(_raw_task_cost(provider, models, config))
        except Exception:
            logger.exception("Could not compute the recommended ceiling for provider %s", provider)
            return default
    return default


def _config_values(body: AiProviderSave, api_key: str, config: ConfigStore, ceiling: float) -> dict[str, str]:
    chosen = body.models.as_dict()
    values: dict[str, str] = {
        "HENCHMEN_LLM_PROVIDER": body.provider,
        "HENCHMEN_LLM_CHAT_MODEL": chosen["light"],
        _CEILING_KEY: f"{ceiling:g}",
    }
    if not config.is_set("HENCHMEN_PROVIDER"):
        # Desktop installs run everything locally; the seeded env default does not count (D-P8).
        values["HENCHMEN_PROVIDER"] = "local"
    for tier, field_name in TIER_FIELDS[body.provider].items():
        values[_env_key(field_name)] = chosen[_tier_key(tier)]
    key_setting = _API_KEY_SETTINGS.get(body.provider)
    if key_setting is not None:
        values[key_setting] = api_key
    elif body.provider == "local":
        values["HENCHMEN_LLM_OLLAMA_BASE_URL"] = body.ollama_base_url
        values["HENCHMEN_LLM_OLLAMA_MODEL"] = chosen["complex"]
    elif body.provider == "gcp":
        values["HENCHMEN_GCP_PROJECT_ID"] = body.gcp_project_id
        values["HENCHMEN_GCP_REGION"] = body.gcp_region
    else:
        values["HENCHMEN_AWS_REGION"] = body.aws_region
    return values


@router.get("")
async def current(config: ConfigDep) -> StepSuccess:
    """Provider options and what is saved now (secrets masked)."""
    provider = normalize_llm_provider(config.get("HENCHMEN_LLM_PROVIDER"))
    models = _saved_models(config, provider)
    key_setting = _API_KEY_SETTINGS.get(provider)
    credential = config.masked([key_setting])[key_setting] if key_setting else ""
    return StepSuccess(
        step=STEP,
        details={
            "providers": list(PROVIDER_OPTIONS),
            "provider": provider,
            "models": models,
            "credential": credential,
            "task_cost_ceiling_usd": _ceiling(config, provider, models),
        },
    )


@router.post("/validate")
async def validate(body: AiCredentials, config: ConfigDep) -> StepSuccess | StepFailure:
    """Check the credential, list reachable models, recommend one per tier and price a typical task."""
    _, models, failure = await _verify(body, config)
    if failure is not None:
        return failure
    recommended = recommended_models(body.provider, models)
    priced = _price_or_none(body.provider, recommended, config)
    if priced is None:
        return step_failed(STEP, _COULD_NOT_ESTIMATE_PROBLEM)
    raw_estimate, estimate = priced
    recommended_ceiling = recommended_ceiling_usd(raw_estimate)
    return StepSuccess(
        step=STEP,
        details={
            "provider": body.provider,
            "models": models,
            "recommended": recommended,
            "estimated_cost_per_task_usd": estimate,
            "recommended_task_cost_ceiling_usd": recommended_ceiling,
            "spending_limit_explanation": _spending_limit_explanation(estimate, recommended_ceiling),
        },
    )


@router.post("")
async def save(body: AiProviderSave, config: ConfigDep, setup: SetupDep) -> StepSuccess | StepFailure:
    """Re-validate, save the provider configuration and complete the step."""
    api_key, models, failure = await _verify(body, config)
    if failure is not None:
        return failure
    chosen = body.models.as_dict()
    unknown = [
        StepProblem(
            field=f"models.{tier}",
            message=f"{model} is not available to this account.",
            action="Pick one of the listed models.",
        )
        for tier, model in chosen.items()
        if model not in models
    ]
    if unknown:
        return step_failed(STEP, *unknown)

    priced = _price_or_none(body.provider, chosen, config)
    if priced is None:
        return step_failed(STEP, _COULD_NOT_ESTIMATE_PROBLEM)
    raw_estimate, estimate = priced
    recommended_ceiling = recommended_ceiling_usd(raw_estimate)
    ceiling = body.task_cost_ceiling_usd if body.task_cost_ceiling_usd is not None else recommended_ceiling
    ceiling_below_estimate = ceiling < raw_estimate

    previous_provider = normalize_llm_provider(config.get("HENCHMEN_LLM_PROVIDER"))
    values = _config_values(body, api_key, config, ceiling)
    try:
        # Both writes happen under one lock acquisition (never `await` inside
        # it) so a concurrent writer can never observe the stale key sitting
        # alongside the new provider's configuration.
        with config.locked():
            if previous_provider and previous_provider != body.provider:
                stale_key = _API_KEY_SETTINGS.get(previous_provider)
                if stale_key is not None:
                    config.unset([stale_key])
            config.update(values, section=CONFIG_SECTION)
    except ConfigStoreError as exc:
        text = str(exc)
        offending_key = next((key for key in values if key in text), None)
        field = _field_for_key(body.provider, offending_key) if offending_key else None
        logger.warning("Rejected AI provider save: %s", text)
        return step_failed(STEP, StepProblem(field=field, message=text, action="Check the values and try again."))

    details: dict[str, Any] = {
        "provider": body.provider,
        "models": chosen,
        "credential": CONFIGURED if body.provider in _API_KEY_SETTINGS else "",
        "task_cost_ceiling_usd": ceiling,
        "estimated_cost_per_task_usd": estimate,
        "ceiling_below_estimate": ceiling_below_estimate,
    }
    if ceiling_below_estimate:
        details["warning"] = (
            f"This limit (${ceiling:.2f}) is below the estimated cost of a typical feature task "
            f"(${estimate:.2f}). Henchmen will stop a task before it goes over the limit."
        )
    return step_succeeded(setup, STEP, details)
