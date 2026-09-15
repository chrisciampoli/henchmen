"""``henchmen init`` — interactive setup wizard.

Walks a self-hoster through every choice Henchmen needs — deployment mode,
LLM provider and per-tier models, GitHub, Slack (including which channel the
bot joins), Jira and limits — validating each credential live and writing
the result to ``.env.local``. Existing values are offered as defaults so the
wizard can be re-run at any time to change one thing.

The wizard is driven entirely through a :class:`~henchmen.cli.prompts.Prompter`
so it can be tested with scripted answers, and it never echoes secrets.
"""

from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from henchmen.cli import checks
from henchmen.cli.checks import CheckResult, CheckStatus, SlackChannel, SlackScopeError
from henchmen.cli.envfile import EnvFile, is_secret_key
from henchmen.cli.prompts import Choice, ConsolePrompter, PromptAbortedError, Prompter, mask_secret
from henchmen.config.paths import config_file

SECTIONS: tuple[str, ...] = ("mode", "llm", "github", "slack", "jira", "limits")
EXIT_ABORTED = 130
_MAX_ATTEMPTS = 3
_TIERS: tuple[tuple[str, str], ...] = (
    ("complex", "core coding (implement_fix / implement_feature)"),
    ("light", "cheap, fast steps (planning, lint fixes)"),
    ("reasoning", "hard steps (fix_tests, goal decomposition)"),
)

# Registry names on the left; the wizard always writes these canonical values.
_LLM_CHOICES: tuple[Choice, ...] = (
    Choice("anthropic", "Anthropic (Claude API)", "recommended for tool-calling reliability"),
    Choice("openai", "OpenAI API"),
    Choice("local", "Ollama (local models)", "free, experimental — needs a tool-calling-capable model"),
    Choice("gcp", "Vertex AI (Gemini on Google Cloud)", "uses Application Default Credentials"),
    Choice("aws", "AWS Bedrock", "experimental"),
)

# Settings field that holds each provider's tier model, plus a fallback used
# only when Settings cannot be imported (never in a normal install).
_TIER_FIELDS: dict[str, dict[str, tuple[str, str]]] = {
    "anthropic": {
        "complex": ("anthropic_model_complex", "claude-sonnet-5"),
        "light": ("anthropic_model_light", "claude-haiku-4-5"),
        "reasoning": ("anthropic_model_reasoning", "claude-opus-5"),
    },
    "openai": {
        "complex": ("openai_model_complex", "gpt-4.1"),
        "light": ("openai_model_light", "gpt-4.1-mini"),
        "reasoning": ("openai_model_reasoning", "o3"),
    },
    "gcp": {
        "complex": ("vertex_ai_model_complex", "gemini-2.5-pro"),
        "light": ("vertex_ai_model_light", "gemini-2.5-flash"),
        "reasoning": ("vertex_ai_model_reasoning", "gemini-3.1-pro"),
    },
    "local": {
        "complex": ("llm_ollama_model_complex", "qwen2.5-coder:7b"),
        "light": ("llm_ollama_model_light", "qwen2.5:3b"),
        "reasoning": ("llm_ollama_model_reasoning", "deepseek-r1:8b"),
    },
    "aws": {
        "complex": ("bedrock_model_complex", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
        "light": ("bedrock_model_light", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
        "reasoning": ("bedrock_model_reasoning", "us.anthropic.claude-sonnet-4-20250514-v1:0"),
    },
}
_VERTEX_MODELS: tuple[str, ...] = ("gemini-2.5-pro", "gemini-2.5-flash", "gemini-3.1-pro")
_OPENAI_EXCLUDE = (
    "realtime",
    "audio",
    "tts",
    "transcribe",
    "embedding",
    "moderation",
    "image",
    "dall",
    "whisper",
    "search",
    "instruct",
    "babbage",
    "davinci",
)


def _settings_default(field_name: str, fallback: str) -> str:
    """Read a Settings field default so the wizard's recommendations track the code."""
    try:
        from henchmen.config.settings import Settings

        default = Settings.model_fields[field_name].default
    except Exception:
        return fallback
    return str(default) if default else fallback


# ---------------------------------------------------------------------------
# Options and state
# ---------------------------------------------------------------------------


@dataclass
class InitOptions:
    """Command-line options for ``henchmen init``."""

    env_file: Path = field(default_factory=config_file)
    yes: bool = False
    dry_run: bool = False
    sections: tuple[str, ...] = SECTIONS
    timeout: float = checks.DEFAULT_TIMEOUT


@dataclass
class WizardState:
    """Answers collected so far, layered over the existing ``.env.local``."""

    env: EnvFile
    pending: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    results: list[CheckResult] = field(default_factory=list)

    def get(self, key: str, default: str = "") -> str:
        if key in self.pending:
            return self.pending[key]
        return self.env.get(key, default)

    def set(self, key: str, value: str, section: str) -> None:
        self.pending[key] = value
        self.sections[key] = section

    def record(self, result: CheckResult) -> None:
        self.results.append(result)

    @property
    def provider(self) -> str:
        return self.get("HENCHMEN_PROVIDER", "local")

    @property
    def llm_provider(self) -> str:
        return self.get("HENCHMEN_LLM_PROVIDER") or ("local" if self.provider == "local" else self.provider)


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------


def _validate_environment(value: str) -> str | None:
    return None if value in ("dev", "staging", "prod") else "environment must be dev, staging or prod"


def _validate_repo_slug(value: str) -> str | None:
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", value):
        return None
    return "repository must be in owner/repo form"


def _validate_required(value: str) -> str | None:
    return None if value.strip() else "a value is required"


def _validate_url(value: str) -> str | None:
    return None if re.match(r"^https?://", value) else "must start with http:// or https://"


def _validate_float(value: str) -> str | None:
    try:
        return None if float(value) > 0 else "must be greater than 0"
    except ValueError:
        return "must be a number"


def _validate_int(value: str) -> str | None:
    return None if value.isdigit() and int(value) > 0 else "must be a positive integer"


# ---------------------------------------------------------------------------
# Prompt helpers
# ---------------------------------------------------------------------------


def _report(prompter: Prompter, state: WizardState, result: CheckResult) -> None:
    state.record(result)
    if result.status == CheckStatus.OK:
        prompter.ok(f"{result.name}: {result.message}")
    elif result.status == CheckStatus.WARN:
        prompter.warn(f"{result.name}: {result.message}")
    else:
        prompter.fail(f"{result.name}: {result.message}")
    if result.hint and result.status != CheckStatus.OK:
        for line in result.hint.splitlines():
            prompter.info(f"         {line}")


def _validated_secret(
    prompter: Prompter,
    state: WizardState,
    prompt: str,
    default: str,
    check: Callable[[str], CheckResult],
    *,
    yes: bool,
) -> str | None:
    """Ask for a secret and verify it live; re-prompt up to ``_MAX_ATTEMPTS`` times.

    Returns the accepted value, or ``None`` if the user chose to skip.
    """
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        value = default if (yes and default) else prompter.secret(prompt, default=default)
        if not value:
            return None
        result = check(value)
        _report(prompter, state, result)
        if result.status != CheckStatus.FAIL:
            return value
        if yes:
            return value
        if attempt == _MAX_ATTEMPTS:
            break
        prompter.info(f"  Attempt {attempt} of {_MAX_ATTEMPTS} failed — try again (Enter to skip).")
        default = ""
    if prompter.confirm("Keep this value anyway (unverified)?", default=False):
        return value
    return None


def _pick_model(
    prompter: Prompter,
    tier: str,
    description: str,
    available: Sequence[str],
    default: str,
    *,
    yes: bool,
) -> str:
    """Pick one model for a tier from a live list (or free text when no list is available)."""
    if yes:
        return default
    prompt = f"Model for the {tier} tier — {description}"
    if not available:
        return prompter.text(prompt, default=default, validator=_validate_required)
    options = [Choice(model, model) for model in available]
    options.append(Choice("__custom__", "Type a model name"))
    selected_default = default if default in available else None
    if selected_default is None and default:
        options.insert(0, Choice(default, f"{default} (recommended, not in the live list)"))
        selected_default = default
    picked = prompter.choice(prompt, options, default=selected_default)
    if picked == "__custom__":
        return prompter.text("Model name", default=default, validator=_validate_required)
    return picked


def _filter_openai_models(models: Sequence[str]) -> list[str]:
    keep = []
    for model in models:
        lowered = model.lower()
        if not lowered.startswith(("gpt-", "o1", "o3", "o4")):
            continue
        if any(marker in lowered for marker in _OPENAI_EXCLUDE):
            continue
        keep.append(model)
    return keep


def _gcloud_project() -> str:
    """Best-effort ``gcloud config get-value project``; empty when unavailable."""
    try:
        result = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    value = (result.stdout or "").strip()
    return "" if value in ("", "(unset)") else value


def _git_config(key: str) -> str:
    try:
        result = subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    return (result.stdout or "").strip() if result.returncode == 0 else ""


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


def section_mode(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[1/6] Deployment mode")
    current = state.get("HENCHMEN_PROVIDER", "local")
    provider = (
        current
        if options.yes
        else prompter.choice(
            "Where will Henchmen run?",
            [
                Choice("local", "local — Docker on this machine, LLM via API", "recommended to start"),
                Choice("gcp", "gcp — Google Cloud Run + Firestore + Pub/Sub"),
                Choice("aws", "aws — AWS (experimental, community supported)"),
            ],
            default=current,
        )
    )
    state.set("HENCHMEN_PROVIDER", provider, "Provider")
    env_default = state.get("HENCHMEN_ENVIRONMENT", "dev")
    environment = (
        env_default
        if options.yes
        else prompter.text("Environment name", default=env_default, validator=_validate_environment)
    )
    state.set("HENCHMEN_ENVIRONMENT", environment, "Provider")

    if provider == "gcp":
        project_default = state.get("HENCHMEN_GCP_PROJECT_ID") or _gcloud_project()
        project = (
            project_default
            if options.yes and project_default
            else prompter.text("GCP project ID", default=project_default, validator=_validate_required)
        )
        state.set("HENCHMEN_GCP_PROJECT_ID", project, "GCP")
        region_default = state.get("HENCHMEN_GCP_REGION", "us-central1")
        region = (
            region_default
            if options.yes
            else prompter.text("GCP region", default=region_default, validator=_validate_required)
        )
        state.set("HENCHMEN_GCP_REGION", region, "GCP")
    elif provider == "aws":
        region_default = state.get("HENCHMEN_AWS_REGION", "us-east-1")
        region = (
            region_default
            if options.yes
            else prompter.text("AWS region", default=region_default, validator=_validate_required)
        )
        state.set("HENCHMEN_AWS_REGION", region, "AWS")


def section_llm(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[2/6] LLM provider and models")
    current = state.llm_provider
    llm = (
        current
        if options.yes
        else prompter.choice("Which LLM provider should operatives use?", list(_LLM_CHOICES), default=current)
    )
    state.set("HENCHMEN_LLM_PROVIDER", llm, "LLM")

    available: list[str] = []
    if llm == "anthropic":
        key = _validated_secret(
            prompter,
            state,
            "Anthropic API key",
            state.get("HENCHMEN_ANTHROPIC_API_KEY"),
            lambda value: checks.check_anthropic_key(value, timeout=options.timeout),
            yes=options.yes,
        )
        if key:
            state.set("HENCHMEN_ANTHROPIC_API_KEY", key, "LLM")
            available = checks.list_anthropic_models(key, timeout=options.timeout)
    elif llm == "openai":
        key = _validated_secret(
            prompter,
            state,
            "OpenAI API key",
            state.get("HENCHMEN_OPENAI_API_KEY"),
            lambda value: checks.check_openai_key(value, timeout=options.timeout),
            yes=options.yes,
        )
        if key:
            state.set("HENCHMEN_OPENAI_API_KEY", key, "LLM")
            available = _filter_openai_models(checks.list_openai_models(key, timeout=options.timeout))
    elif llm == "local":
        url_default = state.get("HENCHMEN_LLM_OLLAMA_BASE_URL", "http://localhost:11434")
        base_url = (
            url_default
            if options.yes
            else prompter.text("Ollama base URL", default=url_default, validator=_validate_url)
        )
        state.set("HENCHMEN_LLM_OLLAMA_BASE_URL", base_url, "LLM")
        _report(prompter, state, checks.check_ollama(base_url, timeout=options.timeout))
        available = checks.list_ollama_models(base_url, timeout=options.timeout)
        if not available:
            prompter.info(f"  Recommended models: {', '.join(checks.RECOMMENDED_OLLAMA_MODELS)} (ollama pull <name>)")
    elif llm == "gcp":
        project = state.get("HENCHMEN_GCP_PROJECT_ID") or _gcloud_project()
        if not project and not options.yes:
            project = prompter.text("GCP project ID for Vertex AI", default="", validator=_validate_required)
        if project:
            state.set("HENCHMEN_GCP_PROJECT_ID", project, "GCP")
        region = state.get("HENCHMEN_GCP_REGION", "us-central1")
        state.set("HENCHMEN_GCP_REGION", region, "GCP")
        _report(prompter, state, checks.check_vertex(project, region, timeout=options.timeout))
        available = list(_VERTEX_MODELS)
    elif llm == "aws":
        region_default = state.get("HENCHMEN_AWS_REGION", "us-east-1")
        region = (
            region_default
            if options.yes
            else prompter.text("AWS region for Bedrock", default=region_default, validator=_validate_required)
        )
        state.set("HENCHMEN_AWS_REGION", region, "AWS")
        prompter.warn("Bedrock support is experimental; model IDs are not validated live.")

    tier_fields = _TIER_FIELDS[llm]
    picked: dict[str, str] = {}
    for tier, description in _TIERS:
        field_name, fallback = tier_fields[tier]
        env_key = f"HENCHMEN_{field_name.upper()}"
        default = state.get(env_key) or _settings_default(field_name, fallback)
        if llm == "local" and not state.get(env_key):
            default = state.get("HENCHMEN_LLM_OLLAMA_MODEL") or (
                available[0] if available and fallback not in available else fallback
            )
        model = _pick_model(prompter, tier, description, available, default, yes=options.yes)
        picked[tier] = model
        state.set(env_key, model, "LLM")
    if llm == "local":
        state.set("HENCHMEN_LLM_OLLAMA_MODEL", picked["complex"], "LLM")

    chat_default = state.get("HENCHMEN_LLM_CHAT_MODEL") or picked["light"]
    chat_model = (
        chat_default
        if options.yes
        else _pick_model(
            prompter,
            "chat",
            "model behind `henchmen chat` (task builder)",
            sorted(set(picked.values())),
            chat_default,
            yes=False,
        )
    )
    state.set("HENCHMEN_LLM_CHAT_MODEL", chat_model, "LLM")


def section_github(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[3/6] GitHub")
    token = _validated_secret(
        prompter,
        state,
        "GitHub personal access token (classic, 'repo' scope)",
        state.get("HENCHMEN_GITHUB_TOKEN"),
        lambda value: checks.check_github_token(value, timeout=options.timeout),
        yes=options.yes,
    )
    if token:
        state.set("HENCHMEN_GITHUB_TOKEN", token, "GitHub")

    repo_default = state.get("HENCHMEN_GITHUB_DEFAULT_REPO")
    org_default = state.get("HENCHMEN_GITHUB_DEFAULT_ORG")
    if repo_default and "/" not in repo_default and org_default:
        repo_default = f"{org_default}/{repo_default}"
    repo = (
        repo_default
        if options.yes and repo_default
        else prompter.text(
            "Default target repository (owner/repo)", default=repo_default, validator=_validate_repo_slug
        )
    )
    if repo:
        state.set("HENCHMEN_GITHUB_DEFAULT_REPO", repo, "GitHub")
        state.set("HENCHMEN_GITHUB_DEFAULT_ORG", repo.split("/", 1)[0], "GitHub")
        if token:
            _report(prompter, state, checks.check_github_repo(token, repo, timeout=options.timeout))

    name_default = state.get("HENCHMEN_GIT_AUTHOR_NAME") or "Henchmen Operative"
    email_default = state.get("HENCHMEN_GIT_AUTHOR_EMAIL") or "henchmen-operative@noreply.local"
    name = (
        name_default
        if options.yes
        else prompter.text("Git author name for operative commits", default=name_default, validator=_validate_required)
    )
    email = (
        email_default
        if options.yes
        else prompter.text(
            "Git author email for operative commits", default=email_default, validator=_validate_required
        )
    )
    state.set("HENCHMEN_GIT_AUTHOR_NAME", name, "Git identity")
    state.set("HENCHMEN_GIT_AUTHOR_EMAIL", email, "Git identity")


def section_slack(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[4/6] Slack (optional)")
    configured = bool(state.get("HENCHMEN_SLACK_BOT_TOKEN"))
    if options.yes:
        wanted = configured
    else:
        wanted = prompter.confirm("Connect Slack so you can @mention Henchmen in a channel?", default=configured)
    if not wanted:
        prompter.info("  Skipping Slack.")
        return

    bot_token = _validated_secret(
        prompter,
        state,
        "Slack bot token (xoxb-…)",
        state.get("HENCHMEN_SLACK_BOT_TOKEN"),
        lambda value: checks.check_slack_bot_token(value, timeout=options.timeout),
        yes=options.yes,
    )
    if not bot_token:
        prompter.info("  Skipping Slack.")
        return
    state.set("HENCHMEN_SLACK_BOT_TOKEN", bot_token, "Slack")

    app_token = _validated_secret(
        prompter,
        state,
        "Slack app-level token for Socket Mode (xapp-…)",
        state.get("HENCHMEN_SLACK_APP_TOKEN"),
        lambda value: checks.check_slack_app_token(value, timeout=options.timeout),
        yes=options.yes,
    )
    if app_token:
        state.set("HENCHMEN_SLACK_APP_TOKEN", app_token, "Slack")

    signing_default = state.get("HENCHMEN_SLACK_SIGNING_SECRET")
    signing = (
        signing_default
        if options.yes
        else prompter.secret("Slack signing secret (optional, for HTTP webhooks)", default=signing_default)
    )
    if signing:
        state.set("HENCHMEN_SLACK_SIGNING_SECRET", signing, "Slack")

    _choose_slack_channel(prompter, state, options, bot_token)


def _choose_slack_channel(prompter: Prompter, state: WizardState, options: InitOptions, bot_token: str) -> None:
    current = state.get("HENCHMEN_SLACK_NOTIFICATION_CHANNEL")
    channels: list[SlackChannel] = []
    try:
        channels = checks.list_slack_channels(bot_token, timeout=options.timeout)
    except SlackScopeError as exc:
        prompter.warn(f"Cannot list channels: {exc}. Add the scope under OAuth & Permissions and reinstall the app.")

    if options.yes:
        channel_id = current
    elif channels:
        choices = [Choice(c.id, c.display, c.id) for c in channels]
        choices.append(Choice("__skip__", "Skip — configure later"))
        channel_id = prompter.choice(
            "Channel for Henchmen to join and post status updates", choices, default=current or None
        )
        if channel_id == "__skip__":
            channel_id = ""
    else:
        channel_id = prompter.text(
            "Slack channel ID to post status updates to (e.g. C0123ABCD, Enter to skip)", default=current
        )

    if not channel_id:
        return
    state.set("HENCHMEN_SLACK_NOTIFICATION_CHANNEL", channel_id, "Slack")
    selected = next((c for c in channels if c.id == channel_id), None)
    if selected is not None and selected.is_member:
        prompter.ok(f"Slack channel: already a member of {selected.display}")
        return
    _report(prompter, state, checks.join_slack_channel(bot_token, channel_id, timeout=options.timeout))


def section_jira(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[5/6] Jira (optional)")
    configured = bool(state.get("HENCHMEN_JIRA_BASE_URL"))
    wanted = configured if options.yes else prompter.confirm("Receive tasks from Jira webhooks?", default=configured)
    if not wanted:
        prompter.info("  Skipping Jira.")
        return
    url_default = state.get("HENCHMEN_JIRA_BASE_URL")
    base_url = (
        url_default
        if options.yes and url_default
        else prompter.text("Jira base URL", default=url_default, validator=_validate_url)
    )
    email_default = state.get("HENCHMEN_JIRA_EMAIL")
    email = (
        email_default
        if options.yes and email_default
        else prompter.text("Jira account email", default=email_default, validator=_validate_required)
    )
    token = _validated_secret(
        prompter,
        state,
        "Jira API token",
        state.get("HENCHMEN_JIRA_API_TOKEN"),
        lambda value: checks.check_jira(base_url, email, value, timeout=options.timeout),
        yes=options.yes,
    )
    state.set("HENCHMEN_JIRA_BASE_URL", base_url, "Jira")
    state.set("HENCHMEN_JIRA_EMAIL", email, "Jira")
    if token:
        state.set("HENCHMEN_JIRA_API_TOKEN", token, "Jira")
    key_default = state.get("HENCHMEN_JIRA_PROJECT_KEY")
    project_key = (
        key_default if options.yes else prompter.text("Default Jira project key (optional)", default=key_default)
    )
    if project_key:
        state.set("HENCHMEN_JIRA_PROJECT_KEY", project_key.upper(), "Jira")


def section_limits(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("[6/6] Limits (optional)")
    if options.yes or not prompter.confirm("Tune cost and time limits for operatives?", default=False):
        prompter.info("  Keeping defaults.")
        return
    cost_default = state.get("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD") or _settings_default(
        "operative_task_cost_ceiling_usd", "6.0"
    )
    cost = prompter.text("Maximum spend per task (USD)", default=cost_default, validator=_validate_float)
    state.set("HENCHMEN_OPERATIVE_TASK_COST_CEILING_USD", cost, "Limits")
    wall_default = state.get("HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS") or _settings_default(
        "operative_wallclock_ceiling_seconds", "1800"
    )
    wall = prompter.text(
        "Maximum wall-clock time per operative (seconds)", default=wall_default, validator=_validate_int
    )
    state.set("HENCHMEN_OPERATIVE_WALLCLOCK_CEILING_SECONDS", wall, "Limits")
    tokens_default = state.get("HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS") or _settings_default(
        "operative_max_output_tokens", "16384"
    )
    tokens = prompter.text("Maximum output tokens per LLM call", default=tokens_default, validator=_validate_int)
    state.set("HENCHMEN_OPERATIVE_MAX_OUTPUT_TOKENS", tokens, "Limits")


_SECTION_RUNNERS: dict[str, Callable[[Prompter, WizardState, InitOptions], None]] = {
    "mode": section_mode,
    "llm": section_llm,
    "github": section_github,
    "slack": section_slack,
    "jira": section_jira,
    "limits": section_limits,
}


# ---------------------------------------------------------------------------
# Summary, write, next steps
# ---------------------------------------------------------------------------


def _print_summary(prompter: Prompter, state: WizardState) -> None:
    prompter.info("")
    prompter.info("Summary")
    width = max((len(k) for k in state.pending), default=10)
    for key, value in state.pending.items():
        shown = mask_secret(value) if is_secret_key(key) else value
        prompter.info(f"  {key.ljust(width)}  {shown}")


def _gcloud_secret_commands(state: WizardState) -> list[str]:
    """Commands to seed Secret Manager for gcp deployments (secrets are never printed)."""
    project = state.get("HENCHMEN_GCP_PROJECT_ID")
    environment = state.get("HENCHMEN_ENVIRONMENT", "dev")
    mapping = {
        "HENCHMEN_GITHUB_TOKEN": "github-token",
        "HENCHMEN_SLACK_BOT_TOKEN": "slack-bot-token",
        "HENCHMEN_SLACK_APP_TOKEN": "slack-app-token",
        "HENCHMEN_SLACK_SIGNING_SECRET": "slack-signing-secret",
        "HENCHMEN_JIRA_API_TOKEN": "jira-api-token",
    }
    commands = []
    for key, secret in mapping.items():
        if state.get(key):
            commands.append(
                f"printf '%s' \"${key}\" | gcloud secrets versions add henchmen-{environment}-{secret} "
                f"--project={project} --data-file=-"
            )
    return commands


def _print_next_steps(prompter: Prompter, state: WizardState, options: InitOptions) -> None:
    prompter.info("")
    prompter.info("Next steps:")
    if state.provider == "gcp":
        commands = _gcloud_secret_commands(state)
        if commands:
            prompter.info("  Seed Secret Manager (export the variables from .env.local first):")
            for command in commands:
                prompter.info(f"    {command}")
        prompter.info("  Then follow docs/deploy-gcp.md to apply Terraform and push images.")
    else:
        prompter.info("  henchmen doctor            # verify the environment end to end")
        prompter.info("  henchmen build-operative   # build the local operative image (first run only)")
        prompter.info("  henchmen serve             # start Dispatch + Mastermind + Forge")
        prompter.info("  henchmen chat              # build and dispatch your first task")


def run_init(prompter: Prompter, options: InitOptions) -> int:
    """Run the wizard. Returns a process exit code."""
    env = EnvFile.load(options.env_file)
    state = WizardState(env=env)
    prompter.info("henchmen init — interactive setup")
    if env.exists:
        prompter.info(
            f"Found {options.env_file} ({len(env.keys())} keys). Existing values are offered as defaults; Enter keeps them."
        )
    else:
        prompter.info(f"No {options.env_file} yet — it will be created.")

    try:
        for section in options.sections:
            _SECTION_RUNNERS[section](prompter, state, options)
        _print_summary(prompter, state)
        if options.dry_run:
            prompter.info("")
            prompter.info(f"--dry-run: {options.env_file} not written. Resulting file:")
            preview = EnvFile.from_text(env.render(), path=options.env_file)
            for key, value in state.pending.items():
                preview.set(key, value, section=state.sections.get(key))
            for line in preview.render().splitlines():
                prompter.info(f"  {line}")
            return 0
        if not options.yes and not prompter.confirm(f"Write these values to {options.env_file}?", default=True):
            prompter.info("Nothing written.")
            return 1
    except PromptAbortedError:
        prompter.info("Aborted — nothing written.")
        return EXIT_ABORTED

    for key, value in state.pending.items():
        env.set(key, value, section=state.sections.get(key))
    backup = env.write(backup=True)
    prompter.ok(f"Wrote {options.env_file}" + (f" (previous copy saved to {backup.name})" if backup else ""))
    failures = [r for r in state.results if r.is_failure]
    if failures:
        prompter.warn(f"{len(failures)} check(s) failed during setup — run `henchmen doctor` after fixing them.")
    _print_next_steps(prompter, state, options)
    return 0


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def add_init_arguments(parser: argparse.ArgumentParser) -> None:
    """Register ``henchmen init`` flags on an argparse sub-parser."""
    parser.add_argument(
        "--env-file",
        default=None,
        help="File to write (default: .env.local, or henchmen.env inside HENCHMEN_DATA_DIR)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Accept defaults and existing values without prompting"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show the resulting file without writing it")
    parser.add_argument(
        "--section",
        action="append",
        choices=SECTIONS,
        help="Run only this section (repeatable). Default: all sections in order.",
    )


def options_from_args(args: argparse.Namespace) -> InitOptions:
    sections = tuple(getattr(args, "section", None) or SECTIONS)
    return InitOptions(
        env_file=Path(args.env_file) if getattr(args, "env_file", None) else config_file(),
        yes=bool(getattr(args, "yes", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        sections=sections,
    )


def run_init_cli(args: argparse.Namespace, prompter: Prompter | None = None) -> int:
    """Entry point used by ``henchmen init`` / ``henchmen setup``."""
    return run_init(prompter or ConsolePrompter(), options_from_args(args))
