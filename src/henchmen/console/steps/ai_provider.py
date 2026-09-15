"""Console step 1: choose, verify and price the AI provider (spec Section 4, Section 5.4).

Credentials are checked with the same ``cli.checks`` functions ``henchmen
init`` and ``henchmen doctor`` use, so the Console and the CLI accept exactly
the same keys. The blocking SDK calls run in a worker thread. The recommended
model per tier is the ``Settings`` default for that tier, and the per-task
cost estimate prices the executor's own most expensive first-task node
(``implement_feature``) through
``mastermind.scheme_executor.executor.estimate_feature_task_cost`` --
which itself goes through ``providers.pricing.estimate_cost_for_settings`` --
so the number shown here always agrees with the cost gate that would
otherwise refuse the task (ruling C2). There is no second price table or
token profile here.

A save re-validates the credential (an earlier ``/validate`` is never
trusted), writes through ``ConfigStore`` and only then records the step
complete.
"""

from __future__ import annotations

import math
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus
from henchmen.config.settings import Settings
from henchmen.console.check_problems import problem_from_check
from henchmen.console.config_store import CONFIGURED, ConfigStore
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
from henchmen.providers.tiers import TIER_FIELDS

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
    recommended ceiling for the chosen models (ruling C2). When given below
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


def recommended_models(provider: str, available: list[str]) -> dict[str, str]:
    """The ``Settings`` default model for each tier (Ollama falls back to a pulled model)."""
    picks: dict[str, str] = {}
    for tier, field_name in TIER_FIELDS[provider].items():
        default = str(Settings.model_fields[field_name].default or "")
        if not default and provider == "local":
            fallback = str(Settings.model_fields["llm_ollama_model"].default or "")
            default = fallback if (fallback in available or not available) else available[0]
        picks[_tier_key(tier)] = default
    return picks


def estimate_task_cost(provider: str, models: dict[str, str]) -> float:
    """USD cost of the executor's most expensive first-task node with ``models``, rounded to cents."""
    overrides: dict[str, Any] = {"_env_file": None, "provider": "local", "llm_provider": provider}
    for tier, field_name in TIER_FIELDS[provider].items():
        overrides[field_name] = models[_tier_key(tier)]
    settings = Settings(**overrides)
    return round(estimate_feature_task_cost(settings), 2)


def recommended_ceiling_usd(estimate: float) -> float:
    """A per-task spending limit that covers ``estimate`` with headroom (ruling C2)."""
    default = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)
    return float(max(default, math.ceil(estimate * 1.5)))


def _spending_limit_explanation(estimate: float, recommended: float) -> str:
    return (
        f"A typical feature task can cost up to about ${estimate:.2f} with these models. "
        f"We suggest a limit of ${recommended:.0f} per task; Henchmen stops a task before it would go over."
    )


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
            pass
    default = float(Settings.model_fields["operative_task_cost_ceiling_usd"].default)
    if provider in TIER_FIELDS and all(models.values()):
        try:
            return recommended_ceiling_usd(estimate_task_cost(provider, models))
        except Exception:
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
    provider = config.get("HENCHMEN_LLM_PROVIDER")
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
    estimate = estimate_task_cost(body.provider, recommended)
    recommended_ceiling = recommended_ceiling_usd(estimate)
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

    estimate = estimate_task_cost(body.provider, chosen)
    recommended_ceiling = recommended_ceiling_usd(estimate)
    ceiling = body.task_cost_ceiling_usd if body.task_cost_ceiling_usd is not None else recommended_ceiling
    ceiling_below_estimate = ceiling < estimate

    config.update(_config_values(body, api_key, config, ceiling), section=CONFIG_SECTION)
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
