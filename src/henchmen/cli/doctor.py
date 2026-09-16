"""`henchmen doctor` — self-check CLI command.

Runs a series of diagnostic checks to verify that the local environment is
ready to run Henchmen. Exits non-zero if any check fails.

Everything is derived from :class:`~henchmen.config.settings.Settings` (which
reads ``.env.local`` then ``.env``) rather than from raw ``os.environ``, so
doctor sees exactly the configuration ``henchmen serve`` / ``eval`` / ``chat``
will see. The live credential probes are the same ones ``henchmen init`` runs
— they live in :mod:`henchmen.cli.checks` — and are skipped with ``--offline``.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus
from henchmen.config.settings import DEFAULT_LOCAL_OPERATIVE_IMAGE
from henchmen.utils.repositories import DEFAULT_REPO_PROBLEM, default_repository, is_owner_name

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

__all__ = [
    "CheckResult",
    "CheckStatus",
    "add_doctor_arguments",
    "check_docker",
    "check_env_file",
    "check_git_identity",
    "check_github",
    "check_jira",
    "check_llm_credentials",
    "check_model_pricing",
    "check_model_tiers",
    "check_operative_image",
    "check_python_version",
    "check_runtime_config",
    "check_settings",
    "check_slack",
    "load_settings",
    "run_doctor",
    "run_doctor_cli",
]


# ---------------------------------------------------------------------------
# Individual checks
# ---------------------------------------------------------------------------


def check_python_version() -> CheckResult:
    """Verify Python >= 3.12."""
    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 12):
        return CheckResult(
            name="Python version",
            status=CheckStatus.OK,
            message=f"Python {major}.{minor} detected",
        )
    return CheckResult(
        name="Python version",
        status=CheckStatus.FAIL,
        message=f"Henchmen requires Python >= 3.12; found {major}.{minor}",
        hint="Install Python 3.12 from https://www.python.org/downloads/",
    )


def check_docker() -> CheckResult:
    """Verify Docker is installed and the daemon is reachable."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message="Docker CLI not found on PATH",
            hint="Install Docker Desktop from https://docs.docker.com/get-docker/",
        )
    except subprocess.TimeoutExpired:
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message="docker info timed out after 10s",
            hint="Is the Docker daemon running? Check with `docker ps`.",
        )

    if result.returncode != 0:
        err = (result.stderr or "").strip().splitlines()[0] if result.stderr else "unknown error"
        return CheckResult(
            name="Docker",
            status=CheckStatus.FAIL,
            message=f"Docker CLI present but daemon unreachable: {err}",
            hint="Start Docker Desktop or the docker service, then rerun `henchmen doctor`.",
        )

    # Extract the server version line for a friendly OK message.
    version = "running"
    for line in (result.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("Server Version"):
            version = line.split(":", 1)[1].strip()
            break
    return CheckResult(
        name="Docker",
        status=CheckStatus.OK,
        message=f"Docker daemon reachable (version {version})",
    )


def check_git_identity() -> CheckResult:
    """Verify ``git config user.name`` and ``user.email`` are set."""
    try:
        name_result = subprocess.run(
            ["git", "config", "--get", "user.name"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        email_result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return CheckResult(
            name="Git identity",
            status=CheckStatus.FAIL,
            message="git CLI not found on PATH",
            hint="Install git from https://git-scm.com/",
        )

    name = (name_result.stdout or "").strip()
    email = (email_result.stdout or "").strip()
    if name_result.returncode != 0 or email_result.returncode != 0 or not name or not email:
        return CheckResult(
            name="Git identity",
            status=CheckStatus.FAIL,
            message="git user.name or user.email is not configured",
            hint="Set both:\n  git config --global user.name 'Your Name'\n  git config --global user.email 'you@example.com'",
        )
    return CheckResult(
        name="Git identity",
        status=CheckStatus.OK,
        message=f"{name} <{email}>",
    )


def check_env_file() -> CheckResult:
    """Check that a ``.env.local`` (or ``.env`` / ``.env.example``) is discoverable."""
    from henchmen.config.paths import config_file, data_dir

    if data_dir() is not None:
        target = config_file()
        if target.is_file():
            return CheckResult(name="Config file", status=CheckStatus.OK, message=f"Found {target}")
        return CheckResult(
            name="Config file",
            status=CheckStatus.WARN,
            message=f"{target} does not exist yet",
            hint="Finish setup in the Henchmen Console, or run `henchmen init`.",
        )

    cwd = Path.cwd()
    if (cwd / ".env.local").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.OK,
            message="Found .env.local in current directory",
        )
    if (cwd / ".env").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.WARN,
            message="No .env.local — using .env as fallback",
            hint="Run `henchmen init` to generate a .env.local.",
        )
    if (cwd / ".env.example").is_file():
        return CheckResult(
            name=".env.local",
            status=CheckStatus.WARN,
            message=".env.example found but .env.local missing",
            hint="Run `henchmen init` to generate a .env.local.",
        )
    return CheckResult(
        name=".env.local",
        status=CheckStatus.WARN,
        message="No .env / .env.local / .env.example in current directory",
        hint="Run `henchmen init` from the directory you start Henchmen in.",
    )


def load_settings() -> tuple[Settings | None, CheckResult]:
    """Build ``Settings`` the way the services do, reporting validation errors.

    Returns ``(settings, result)``; ``settings`` is ``None`` when construction
    failed, in which case ``result`` is a FAIL carrying pydantic's message.
    """
    from henchmen.config.paths import env_files
    from henchmen.config.settings import Settings

    try:
        settings = Settings(_env_file=env_files())  # type: ignore[call-arg]
    except ValueError as exc:  # pydantic ValidationError subclasses ValueError
        detail = str(exc).strip().splitlines()
        first = detail[0] if detail else "invalid settings"
        return None, CheckResult(
            name="Settings",
            status=CheckStatus.FAIL,
            message=f"Settings failed to load: {first}",
            hint="Run `henchmen init` to rewrite .env.local, or fix the offending HENCHMEN_ value by hand.",
        )
    return settings, CheckResult(
        name="Settings",
        status=CheckStatus.OK,
        message=(
            f"provider={settings.provider}, llm={_llm_provider(settings)}, environment={settings.environment.value}"
        ),
    )


def check_settings() -> CheckResult:
    """Construct ``Settings`` so pydantic validation runs exactly as it does at startup."""
    return load_settings()[1]


def _llm_provider(settings: Settings) -> str:
    from henchmen.providers.tiers import active_llm_provider

    return active_llm_provider(settings)


def check_model_tiers(settings: Settings) -> CheckResult:
    """Report the concrete model each tier resolves to for the active provider."""
    from henchmen.models.llm import ModelTier
    from henchmen.providers.tiers import tier_models

    models = tier_models(settings)
    if not models:
        return CheckResult(
            name="Model tiers",
            status=CheckStatus.FAIL,
            message=f"No tier mapping for LLM provider {_llm_provider(settings)!r}",
            hint="Set HENCHMEN_LLM_PROVIDER to one of: gcp, aws, local, openai, anthropic.",
        )
    missing = sorted(tier.value for tier, model in models.items() if not model)
    rendered = ", ".join(f"{tier.value.split('/')[-1]}={models[tier] or '(unset)'}" for tier in ModelTier)
    if missing:
        return CheckResult(
            name="Model tiers",
            status=CheckStatus.FAIL,
            message=f"{rendered} — no model configured for {', '.join(missing)}",
            hint="Run `henchmen init` to pick models, or set the HENCHMEN_*_MODEL_<TIER> variables.",
        )
    return CheckResult(name="Model tiers", status=CheckStatus.OK, message=rendered)


def check_model_pricing(settings: Settings) -> CheckResult:
    """Warn when a tier resolves to a model with no ``PRICE_TABLE`` entry.

    An unpriced model is costed at $0, so ``operative_task_cost_ceiling_usd``
    can never trip for it and the metrics under-report spend. Local (Ollama)
    models are free by design; their runs are bounded by the wall-clock
    ceiling instead.
    """
    from henchmen.providers.pricing import lookup_price
    from henchmen.providers.tiers import tier_models

    name = "Model pricing"
    if _llm_provider(settings) == "local":
        return CheckResult(name, CheckStatus.OK, "local models are free; the wall-clock ceiling bounds each run")
    models = tier_models(settings)
    unpriced = sorted({model for model in models.values() if model and lookup_price(model) is None})
    if unpriced:
        return CheckResult(
            name,
            CheckStatus.WARN,
            f"no price for {', '.join(unpriced)} — cost is recorded as $0 and the task cost ceiling "
            f"(${settings.operative_task_cost_ceiling_usd:.2f}) cannot trip",
            hint="Pick a priced model, or add the model to PRICE_TABLE in src/henchmen/providers/pricing.py.",
        )
    return CheckResult(name, CheckStatus.OK, "every tier model has a price")


def check_llm_credentials(settings: Settings, *, offline: bool = False) -> CheckResult:
    """Verify credentials for the configured LLM provider, live when possible.

    Only ``HENCHMEN_``-prefixed keys count: the providers read
    ``settings.openai_api_key`` / ``settings.anthropic_api_key``, and the lair
    forwards only those into operative containers, so a bare ``OPENAI_API_KEY``
    on the host would never reach a run.
    """
    name = "LLM credentials"
    provider = _llm_provider(settings)

    if provider == "local":
        if offline:
            return CheckResult(name, CheckStatus.OK, f"Ollama at {settings.llm_ollama_base_url} (not probed)")
        return checks.check_ollama(settings.llm_ollama_base_url)

    if provider == "openai":
        if not settings.openai_api_key:
            return CheckResult(
                name,
                CheckStatus.FAIL,
                "HENCHMEN_LLM_PROVIDER=openai but no API key set",
                hint="Set HENCHMEN_OPENAI_API_KEY in .env.local (a bare OPENAI_API_KEY is not read).",
            )
        if offline:
            return CheckResult(name, CheckStatus.OK, "HENCHMEN_OPENAI_API_KEY is set (not verified)")
        return checks.check_openai_key(settings.openai_api_key)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            return CheckResult(
                name,
                CheckStatus.FAIL,
                "HENCHMEN_LLM_PROVIDER=anthropic but no API key set",
                hint="Set HENCHMEN_ANTHROPIC_API_KEY in .env.local (a bare ANTHROPIC_API_KEY is not read).",
            )
        if offline:
            return CheckResult(name, CheckStatus.OK, "HENCHMEN_ANTHROPIC_API_KEY is set (not verified)")
        return checks.check_anthropic_key(settings.anthropic_api_key)

    if provider == "gcp":
        if offline:
            return CheckResult(name, CheckStatus.OK, f"Vertex AI in {settings.gcp_project_id} (not probed)")
        return checks.check_vertex(settings.gcp_project_id, settings.gcp_region)

    if provider == "aws":
        return CheckResult(
            name,
            CheckStatus.WARN,
            "Provider=aws — Bedrock support is experimental",
            hint="Configure an AWS profile with Bedrock InvokeModel permissions.",
        )

    from henchmen.providers.tiers import CANONICAL_LLM_PROVIDERS

    return CheckResult(
        name,
        CheckStatus.FAIL,
        f"Unknown LLM provider {provider!r}",
        hint=f"Valid values: {', '.join(CANONICAL_LLM_PROVIDERS)} (ollama=local, vertex=gcp, bedrock=aws).",
    )


def check_github(settings: Settings, *, offline: bool = False) -> CheckResult:
    """Verify GitHub credentials (GitHub App or token) and, when set, the default target repository.

    The credentials provider decides first: a partly configured App makes every
    token call fail, and ``uses_app`` is False for it, so the problem is reported
    from the provider's own error rather than falling through to "GitHub OK via PAT".
    With an App and a default repository, the token is scoped to that repository,
    so an App that cannot see it fails the check.
    """
    from henchmen.utils.github_auth import GitHubAuthError, GitHubRepositoryAccessError, get_credentials_provider

    name = "GitHub"
    app_hint = "Reconnect GitHub in the Henchmen Console, or check the HENCHMEN_GITHUB_APP_* settings."
    repo = default_repository(settings)
    try:
        provider = get_credentials_provider(settings)
        if provider.uses_app and offline:
            return CheckResult(name, CheckStatus.OK, f"GitHub App {settings.github_app_id} configured (not verified)")
        if provider.uses_app and repo:
            if not is_owner_name(repo):
                return CheckResult(name, CheckStatus.FAIL, f"{DEFAULT_REPO_PROBLEM} (got {repo!r})")
            provider.token(repo)
        else:
            # No network for a PAT (it is returned as configured) or a partly configured App (it raises at once).
            provider.token()
    except GitHubRepositoryAccessError:
        return CheckResult(
            name,
            CheckStatus.FAIL,
            f"the GitHub App can't see {repo}",
            hint="Add the repository to the Henchmen app's repository access on GitHub, or choose another default.",
        )
    except GitHubAuthError as exc:
        return CheckResult(name, CheckStatus.FAIL, f"GitHub App cannot get an installation token: {exc}", hint=app_hint)
    if provider.uses_app:
        if repo:
            return CheckResult(name, CheckStatus.OK, f"GitHub App {settings.github_app_id} can access {repo}")
        return CheckResult(name, CheckStatus.OK, f"GitHub App {settings.github_app_id} can get installation tokens")
    if not settings.github_token:
        return CheckResult(
            name,
            CheckStatus.WARN,
            "no GitHub token configured — operatives cannot clone or open PRs",
            hint="Set HENCHMEN_GITHUB_TOKEN (or GITHUB_TOKEN) to a PAT with the 'repo' scope.",
        )
    if offline:
        return CheckResult(name, CheckStatus.OK, "HENCHMEN_GITHUB_TOKEN is set (not verified)")
    if repo:
        return checks.check_github_repo(settings.github_token, repo)
    return checks.check_github_token(settings.github_token)


def check_slack(settings: Settings, *, offline: bool = False) -> CheckResult:
    """Verify the Slack bot and app-level tokens when Slack intake is configured."""
    name = "Slack"
    if not settings.slack_bot_token and not settings.slack_app_token:
        return CheckResult(name, CheckStatus.OK, "not configured (Slack intake disabled)")
    if offline:
        return CheckResult(name, CheckStatus.OK, "Slack tokens are set (not verified)")
    bot = checks.check_slack_bot_token(settings.slack_bot_token)
    if bot.is_failure:
        return bot
    app = checks.check_slack_app_token(settings.slack_app_token)
    if app.is_failure:
        return app
    return CheckResult(name, CheckStatus.OK, f"{bot.message}; Socket Mode token valid")


def check_jira(settings: Settings, *, offline: bool = False) -> CheckResult:
    """Verify Jira credentials when Jira intake is configured."""
    name = "Jira"
    configured = any((settings.jira_base_url, settings.jira_email, settings.jira_api_token))
    if not configured:
        return CheckResult(name, CheckStatus.OK, "not configured (Jira intake disabled)")
    if offline:
        return CheckResult(name, CheckStatus.OK, "Jira credentials are set (not verified)")
    return checks.check_jira(settings.jira_base_url, settings.jira_email, settings.jira_api_token)


def check_runtime_config(settings: Settings) -> CheckResult:
    """Surface every problem ``Settings.validate_for_runtime`` knows about, and every notice.

    A notice (``Settings.runtime_notices``) is a half-finished setup step, not a
    broken configuration: it never fails the check on its own -- it warns, so it
    is never silently dropped either.
    """
    problems = settings.validate_for_runtime()
    notices = settings.runtime_notices()
    if problems:
        return CheckResult(
            name="Runtime config",
            status=CheckStatus.FAIL,
            message=f"{len(problems)} problem(s): " + " ".join([*problems, *notices]),
            hint="Run `henchmen init` to fix these interactively.",
        )
    if notices:
        return CheckResult(name="Runtime config", status=CheckStatus.WARN, message=" ".join(notices))
    return CheckResult(name="Runtime config", status=CheckStatus.OK, message="no configuration problems found")


def check_operative_image(image: str = DEFAULT_LOCAL_OPERATIVE_IMAGE) -> CheckResult:
    """Check whether the operative Docker image local mode will run is present."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return CheckResult(
            name="Operative image",
            status=CheckStatus.WARN,
            message="Cannot inspect image (docker not available)",
        )
    if result.returncode == 0:
        return CheckResult(name="Operative image", status=CheckStatus.OK, message=f"{image} exists")
    hint = (
        "Run `henchmen build-operative` to build it (~3 min on first run)."
        if image == DEFAULT_LOCAL_OPERATIVE_IMAGE
        else f"Run `docker pull {image}`."
    )
    return CheckResult(
        name="Operative image",
        status=CheckStatus.WARN,
        message=f"{image} is not present",
        hint=hint,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def run_doctor(*, offline: bool = False) -> list[CheckResult]:
    """Run every registered check and return the list of results.

    With ``offline=True`` no network call is made: credentials are reported as
    present/absent without being verified against the provider.
    """
    results = [
        check_python_version(),
        check_docker(),
        check_git_identity(),
        check_env_file(),
    ]
    settings, settings_result = load_settings()
    results.append(settings_result)
    if settings is None:
        return results

    results.append(check_runtime_config(settings))
    results.append(check_model_tiers(settings))
    results.append(check_model_pricing(settings))
    results.append(check_llm_credentials(settings, offline=offline))
    results.append(check_github(settings, offline=offline))
    results.append(check_slack(settings, offline=offline))
    results.append(check_jira(settings, offline=offline))
    results.append(check_operative_image(settings.operative_image or DEFAULT_LOCAL_OPERATIVE_IMAGE))
    return results


def _format_result(result: CheckResult) -> str:
    """Format a single CheckResult for stdout."""
    glyphs = {
        CheckStatus.OK: "[OK]",
        CheckStatus.WARN: "[WARN]",
        CheckStatus.FAIL: "[FAIL]",
    }
    glyph = glyphs[result.status]
    out = f"  {glyph:6s} {result.name}: {result.message}"
    if result.hint:
        for hint_line in result.hint.splitlines():
            out += f"\n         ↳ {hint_line}"
    return out


def add_doctor_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``henchmen doctor`` flags."""
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Skip live credential probes; only report whether values are configured",
    )


def run_doctor_cli(args: argparse.Namespace | None = None) -> int:
    """Run all checks and print a formatted report. Returns exit code."""
    offline = bool(getattr(args, "offline", False))
    results = run_doctor(offline=offline)

    print("henchmen doctor — self-check")
    if offline:
        print("(offline mode: credentials are not verified against providers)")
    print()
    for r in results:
        print(_format_result(r))
    print()

    failures = sum(1 for r in results if r.is_failure)
    warnings = sum(1 for r in results if r.status == CheckStatus.WARN)
    oks = sum(1 for r in results if r.is_ok)

    print(f"Result: {oks} ok, {warnings} warnings, {failures} failures")
    return 0 if failures == 0 else 1
