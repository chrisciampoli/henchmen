"""Global application configuration using pydantic-settings.

Every credential, model choice and limit Henchmen reads comes from this
``Settings`` class (``HENCHMEN_`` prefix, ``.env.local`` then ``.env``).
Components must not read ``os.environ`` for a concept that has a field here.

Token fields accept two spellings: the ``HENCHMEN_``-prefixed name that
``henchmen init`` writes to ``.env.local`` and the bare name that Cloud Run
secret mounts inject (``GITHUB_TOKEN``, ``SLACK_BOT_TOKEN``, ...). When both
are present the ``HENCHMEN_`` name wins.
"""

from enum import StrEnum
from functools import lru_cache
from typing import Literal

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


# Settings forwarded to operative containers (see ``Settings.operative_env``).
_OPERATIVE_ENV_FIELDS: tuple[str, ...] = (
    "provider",
    "environment",
    "llm_provider",
    "gcp_project_id",
    "gcp_region",
    "firestore_database",
    "gcs_bucket_dossier",
    "gcs_bucket_snapshots",
    "git_author_name",
    "git_author_email",
    "github_default_repo",
    "operative_max_system_tokens",
    "operative_max_message_tokens",
    "operative_max_output_tokens",
    "operative_task_cost_ceiling_usd",
    "operative_wallclock_ceiling_seconds",
    "operative_heartbeat_interval_seconds",
    "allow_force_push",
    "vertex_ai_model_complex",
    "vertex_ai_model_light",
    "vertex_ai_model_reasoning",
    "vertex_ai_safety_threshold",
    "rag_corpus_display_name",
    "rag_corpus_region",
    "rag_embedding_model",
    "anthropic_model_complex",
    "anthropic_model_light",
    "anthropic_model_reasoning",
    "openai_model_complex",
    "openai_model_light",
    "openai_model_reasoning",
    "llm_ollama_base_url",
    "llm_ollama_model",
    "llm_ollama_model_complex",
    "llm_ollama_model_light",
    "llm_ollama_model_reasoning",
    "llm_ollama_skip_probe",
    "bedrock_model_complex",
    "bedrock_model_light",
    "bedrock_model_reasoning",
    "aws_region",
    "local_forward_base_url",
)

# Secrets forwarded only when the caller opts in (local Docker mode). In gcp
# mode these arrive through Secret Manager mounts under their bare names.
_OPERATIVE_SECRET_FIELDS: tuple[str, ...] = ("github_token", "openai_api_key", "anthropic_api_key")

# Accepted at the configuration boundary. ``llm_provider`` additionally accepts
# the friendly aliases normalised in ``henchmen.providers.tiers`` (ollama,
# vertex, bedrock, ...) — kept in sync by a unit test.
_VALID_PROVIDERS: frozenset[str] = frozenset({"gcp", "aws", "local"})
_VALID_LLM_PROVIDER_INPUTS: frozenset[str] = frozenset(
    {"gcp", "aws", "local", "openai", "anthropic"}
    | {"ollama", "vertex", "vertexai", "vertex-ai", "vertex_ai", "gemini", "google", "bedrock", "claude"}
)


# terraform/modules/secrets seeds every Secret Manager secret with this value so
# the first apply yields startable Cloud Run revisions. It is published in this
# repository, so a credential still holding it is treated as unset: a secret
# used to *verify* callers (webhook signatures, API and metrics bearer tokens)
# would otherwise accept anyone who has read the Terraform source.
SEEDED_SECRET_PLACEHOLDER = "placeholder-replace-with-a-real-value"

_SEEDED_SECRET_FIELDS: tuple[str, ...] = (
    "github_token",
    "github_webhook_secret",
    "slack_bot_token",
    "slack_app_token",
    "slack_signing_secret",
    "jira_api_token",
    "jira_webhook_secret",
    "metrics_auth_token",
    "dispatch_api_token",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HENCHMEN_",
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # GCP core
    gcp_project_id: str = Field(default="", description="GCP project ID (required for provider=gcp)")
    gcp_region: str = Field(default="us-central1", description="GCP region")
    environment: Environment = Field(default=Environment.DEV, description="Deployment environment")

    # Provider selection
    provider: str = Field(default="gcp", description="Default provider: gcp, aws, or local")
    message_broker_provider: str = Field(default="", description="Override MessageBroker provider")
    document_store_provider: str = Field(default="", description="Override DocumentStore provider")
    object_store_provider: str = Field(default="", description="Override ObjectStore provider")
    container_orchestrator_provider: str = Field(default="", description="Override ContainerOrchestrator provider")
    llm_provider: str = Field(
        default="",
        description=(
            "Override LLM provider: gcp (Vertex AI), aws (Bedrock), local (Ollama), openai, anthropic. "
            "The aliases ollama, vertex and bedrock are accepted."
        ),
    )
    ci_provider: str = Field(default="", description="Override CI provider")

    # Pub/Sub topics (defaults include environment prefix). Only topics some
    # component publishes to or subscribes on have a field here.
    pubsub_topic_task_intake: str = Field(default="", description="Topic Dispatch publishes normalized tasks to")
    pubsub_topic_operative_complete: str = Field(default="", description="Topic operatives publish their reports to")
    pubsub_topic_forge_request: str = Field(default="", description="Topic Mastermind publishes CI/PR requests to")
    pubsub_topic_forge_result: str = Field(default="", description="Topic Forge publishes CI/PR results to")
    pubsub_topic_dead_letter: str = Field(default="", description="Dead-letter topic drained by the watchdog")
    pubsub_topic_embed_request: str = Field(default="", description="Topic Dispatch publishes re-index requests to")
    pubsub_topic_ci_failure: str = Field(default="", description="Topic Dispatch publishes GitHub CI failures to")
    dead_letter_subscription: str = Field(
        default="",
        description="Subscription the watchdog drains dead letters from; empty means <pubsub_topic_dead_letter>-sub",
    )

    def model_post_init(self, __context: object) -> None:
        """Set environment-prefixed defaults for Pub/Sub topics and validate provider requirements."""
        if self.provider not in _VALID_PROVIDERS:
            valid = ", ".join(sorted(_VALID_PROVIDERS))
            msg = f"HENCHMEN_PROVIDER={self.provider!r} is not valid. Choose one of: {valid}."
            raise ValueError(msg)

        for field_name, valid_values in (
            ("message_broker_provider", _VALID_PROVIDERS),
            ("document_store_provider", _VALID_PROVIDERS),
            ("object_store_provider", _VALID_PROVIDERS),
            ("container_orchestrator_provider", _VALID_PROVIDERS),
            ("ci_provider", _VALID_PROVIDERS),
            ("llm_provider", _VALID_LLM_PROVIDER_INPUTS),
        ):
            value = str(getattr(self, field_name))
            if value and value.lower() not in valid_values:
                valid = ", ".join(sorted(valid_values))
                msg = f"HENCHMEN_{field_name.upper()}={value!r} is not valid. Choose one of: {valid}."
                raise ValueError(msg)

        if self.provider == "gcp" and not self.gcp_project_id:
            msg = (
                "HENCHMEN_GCP_PROJECT_ID is required when HENCHMEN_PROVIDER=gcp. "
                "Set it in your .env.local or environment, or run `henchmen init`."
            )
            raise ValueError(msg)

        env = self.environment.value
        defaults = {
            "pubsub_topic_task_intake": f"henchmen-{env}-task-intake",
            "pubsub_topic_operative_complete": f"henchmen-{env}-operative-complete",
            "pubsub_topic_forge_request": f"henchmen-{env}-forge-request",
            "pubsub_topic_forge_result": f"henchmen-{env}-forge-result",
            "pubsub_topic_dead_letter": f"henchmen-{env}-dead-letter",
            "pubsub_topic_embed_request": f"henchmen-{env}-embed-request",
            "pubsub_topic_ci_failure": f"henchmen-{env}-ci-failure",
        }
        for field_name, default_value in defaults.items():
            if not getattr(self, field_name):
                object.__setattr__(self, field_name, default_value)

    # Firestore
    firestore_database: str = Field(default="(default)", description="Firestore database name")

    # GCS buckets
    gcs_bucket_dossier: str = Field(default="", description="GCS bucket for dossier artifacts")
    gcs_bucket_snapshots: str = Field(default="", description="GCS bucket for operative snapshots")

    # Git identity for operative commits
    git_author_email: str = Field(
        default="henchmen-operative@noreply.local", description="Git author email for operative commits"
    )
    git_author_name: str = Field(default="Henchmen Operative", description="Git author name for operative commits")

    # GitHub integration
    github_webhook_secret: str = Field(default="", description="GitHub webhook secret")
    github_token: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_GITHUB_TOKEN", "GITHUB_TOKEN"),
        description="GitHub token (classic PAT with repo scope) used for clone, push, PRs and CI feedback",
    )
    github_default_org: str = Field(default="", description="Default GitHub organization")
    github_default_repo: str = Field(default="", description="Default target repo for tasks (owner/repo)")

    # Slack integration
    slack_bot_token: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_SLACK_BOT_TOKEN", "SLACK_BOT_TOKEN", "HENCHMEN_SLACK_BOT_TOKEN_SECRET"),
        description="Slack bot user OAuth token (xoxb-...)",
    )
    slack_app_token: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_SLACK_APP_TOKEN", "SLACK_APP_TOKEN", "HENCHMEN_SLACK_APP_TOKEN_SECRET"),
        description="Slack app-level token for Socket Mode (xapp-...)",
    )
    slack_signing_secret: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_SLACK_SIGNING_SECRET", "SLACK_SIGNING_SECRET"),
        description="Slack signing secret for HTTP event verification",
    )
    slack_notification_channel: str = Field(
        default="", description="Slack channel ID the bot joins on startup and posts status updates to"
    )

    # Jira integration
    jira_base_url: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_JIRA_BASE_URL", "JIRA_SERVER"),
        description="Jira instance base URL",
    )
    jira_email: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_JIRA_EMAIL", "JIRA_EMAIL"),
        description="Jira service account email",
    )
    jira_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_JIRA_API_TOKEN", "JIRA_API_TOKEN", "HENCHMEN_JIRA_API_TOKEN_SECRET"),
        description="Jira API token",
    )
    jira_project_key: str = Field(default="", description="Default Jira project key")
    jira_repo_field: str = Field(
        default="",
        description=(
            "Jira field ID (e.g. customfield_10042) holding the target repository (owner/repo). Use the field ID "
            "from the Jira instance, not its display name: webhooks only send custom fields as customfield_<number>."
        ),
    )
    jira_branch_field: str = Field(
        default="",
        description=(
            "Jira field ID (e.g. customfield_10043) holding the target branch. Use the field ID from the Jira "
            "instance, not its display name: webhooks only send custom fields as customfield_<number>."
        ),
    )
    jira_webhook_secret: str = Field(
        default="", description="Shared secret for Jira webhook HMAC verification (X-Hub-Signature)"
    )

    # Vertex AI model tiers (Gemini only — no Claude on Vertex AI)
    vertex_ai_model_complex: str = Field(default="gemini-2.5-pro", description="Vertex AI model for the COMPLEX tier")
    vertex_ai_model_light: str = Field(default="gemini-2.5-flash", description="Vertex AI model for the LIGHT tier")
    vertex_ai_model_reasoning: str = Field(
        default="gemini-3.1-pro", description="Vertex AI model for the REASONING tier"
    )

    # Operative context limits (token-based)
    operative_max_system_tokens: int = Field(default=20_000, description="Max tokens for system prompt")
    operative_max_message_tokens: int = Field(default=16_000, description="Max tokens for a single message")
    operative_max_output_tokens: int = Field(
        default=16_384,
        description=(
            "Max output tokens per LLM call. Higher values let the model do more per "
            "request, reducing total request count and amortizing input-token costs."
        ),
    )

    # Operative cost ceilings (task-level ceiling spans all nodes)
    operative_task_cost_ceiling_usd: float = Field(
        default=6.0,
        description="Maximum cumulative cost in USD for a single task across all scheme nodes.",
    )
    operative_wallclock_ceiling_seconds: int = Field(
        default=1800,
        description="Wall-clock ceiling (seconds) per operative; also the cost proxy for free local providers.",
    )

    # Operative liveness (intra-node heartbeat)
    operative_heartbeat_interval_seconds: int = Field(
        default=60,
        description="Interval between intra-node heartbeat writes from the operative to the document store.",
    )

    # Operative git safety
    allow_force_push: bool = Field(
        default=False,
        description="Allow operatives to force-push to non-protected branches (never to main/master/release).",
    )

    # Pub/Sub push authentication (in-app OIDC verification)
    pubsub_oidc_audience: str = Field(
        default="",
        description=(
            "Expected 'aud' claim on OIDC tokens presented by Pub/Sub push subscriptions. "
            "Must match the value configured on the subscription. Empty in DEV disables "
            "verification with a logged warning; empty in STAGING/PROD causes 401."
        ),
    )
    pubsub_oidc_allowed_emails: str = Field(
        default="",
        description=(
            "Comma-separated allow-list of publisher service-account emails. "
            "When set, the OIDC 'email' claim on incoming push requests must match. "
            "Leave empty to allow any valid token for the configured audience."
        ),
    )

    # Vertex AI safety settings
    vertex_ai_safety_threshold: Literal[
        "BLOCK_LOW_AND_ABOVE", "BLOCK_MEDIUM_AND_ABOVE", "BLOCK_ONLY_HIGH", "BLOCK_NONE", "OFF"
    ] = Field(
        default="BLOCK_MEDIUM_AND_ABOVE",
        description=(
            "Gemini safety-filter threshold applied to the harassment, hate speech, sexually explicit "
            "and dangerous content categories on every Vertex AI call"
        ),
    )

    # Vertex AI evaluation
    vertex_ai_evaluation_enabled: bool = Field(default=False, description="Enable post-operative GenAI evaluation")

    # Vertex AI experiments
    vertex_ai_experiments_enabled: bool = Field(default=False, description="Enable Vertex AI Experiments tracking")
    vertex_ai_experiment_name: str = Field(default="henchmen-operatives", description="Vertex AI experiment name")

    # Vertex AI RAG Engine
    rag_corpus_display_name: str = Field(default="henchmen-code", description="RAG corpus display name")
    rag_corpus_region: str = Field(
        default="us-west1", description="GCP region for RAG Engine corpus (may differ from main region)"
    )
    rag_embedding_model: str = Field(default="text-embedding-005", description="Embedding model for RAG corpus")

    # Ollama (local LLM)
    llm_ollama_base_url: str = Field(default="http://localhost:11434", description="Ollama server URL")
    llm_ollama_model: str = Field(
        default="qwen2.5-coder:7b",
        description="Default Ollama model; used for any tier without its own HENCHMEN_LLM_OLLAMA_MODEL_<TIER>",
    )
    llm_ollama_model_complex: str = Field(default="", description="Ollama model for the COMPLEX tier")
    llm_ollama_model_light: str = Field(default="", description="Ollama model for the LIGHT tier")
    llm_ollama_model_reasoning: str = Field(default="", description="Ollama model for the REASONING tier")
    llm_ollama_chat_model: str = Field(
        default="", description="Ollama model for henchmen chat (falls back to llm_chat_model, then llm_ollama_model)"
    )
    llm_ollama_skip_probe: bool = Field(
        default=False,
        description=(
            "Skip the Ollama tool-calling capability probe issued on the first "
            "generate() call with tools. Set to True in CI or when running with "
            "mocked httpx clients that don't mimic a real Ollama server."
        ),
    )

    # Chat (henchmen chat) — provider-agnostic
    llm_chat_model: str = Field(
        default="",
        description="Model behind `henchmen chat`; empty means the active provider's LIGHT tier",
    )

    # AWS settings (used when provider=aws)
    aws_region: str = Field(default="us-east-1", description="AWS region")
    aws_account_id: str = Field(default="", description="AWS account ID")
    aws_resource_prefix: str = Field(default="henchmen", description="Prefix for AWS resource names")
    aws_dynamodb_table: str = Field(default="henchmen", description="DynamoDB table name")
    aws_ecs_cluster: str = Field(default="henchmen", description="ECS cluster name")
    aws_ecs_subnets: str = Field(default="", description="Comma-separated subnet IDs for ECS tasks")
    aws_ecs_security_groups: str = Field(default="", description="Comma-separated security group IDs")
    aws_ecs_execution_role_arn: str = Field(
        default="",
        description=(
            "ARN of the ECS task execution role. Required on Fargate: without it the awslogs log driver and "
            "private image pulls are refused."
        ),
    )

    # Bedrock model tiers (experimental)
    # Defaults are US cross-region inference profiles (``us.`` prefix): Bedrock
    # rejects on-demand invocation of these models by their bare model IDs.
    bedrock_model_complex: str = Field(
        default="us.anthropic.claude-sonnet-4-20250514-v1:0",
        description="Bedrock model ID or inference profile for the COMPLEX tier",
    )
    bedrock_model_light: str = Field(
        default="us.anthropic.claude-haiku-4-5-20251001-v1:0",
        description="Bedrock model ID or inference profile for the LIGHT tier",
    )
    bedrock_model_reasoning: str = Field(
        default="us.anthropic.claude-sonnet-4-20250514-v1:0",
        description="Bedrock model ID or inference profile for the REASONING tier",
    )

    # Direct API keys (used when llm_provider=openai or anthropic)
    openai_api_key: str = Field(default="", description="OpenAI API key")
    anthropic_api_key: str = Field(default="", description="Anthropic API key")

    # OpenAI model tier mapping
    openai_model_complex: str = Field(default="gpt-4.1", description="OpenAI model used for the COMPLEX tier")
    openai_model_light: str = Field(default="gpt-4.1-mini", description="OpenAI model used for the LIGHT tier")
    openai_model_reasoning: str = Field(default="o3", description="OpenAI model used for the REASONING tier")

    # Anthropic model tier mapping (current first-party IDs; never append date suffixes)
    anthropic_model_complex: str = Field(
        default="claude-sonnet-5",
        description="Anthropic model used for the COMPLEX tier",
    )
    anthropic_model_light: str = Field(
        default="claude-haiku-4-5",
        description="Anthropic model used for the LIGHT tier",
    )
    anthropic_model_reasoning: str = Field(
        default="claude-opus-5",
        description="Anthropic model used for the REASONING tier",
    )

    # CI (Cloud Build runs the target repository's checks)
    ci_builder_image: str = Field(
        default="python:3.12",
        description="Container image Cloud Build uses to run the target repository's checks",
    )
    ci_github_token_secret: str = Field(
        default="",
        description=(
            "Secret Manager secret name the CI build reads the GitHub token from to clone private repos; "
            "empty clones anonymously"
        ),
    )

    # Dossier
    dossier_semantic_rerank: bool = Field(
        default=True,
        description=(
            "Rerank semantic code-search results with one light-tier LLM call before building the dossier. "
            "Disable to save that call per task."
        ),
    )

    # Forge
    forge_ci_timeout_seconds: int = Field(
        default=540,
        ge=30,
        le=580,
        description=(
            "Total wall-clock budget in seconds for one Forge CI run. Capped below 600 because Pub/Sub redelivers "
            "a push after its 600s ack deadline, so a run must finish (and ack) before that or it runs twice."
        ),
    )

    # Evals
    eval_db_path: str = Field(
        default="",
        description="SQLite file for `henchmen eval` history; empty means ~/.henchmen/eval/results.db",
    )

    # Local single-process mode (`henchmen serve`)
    local_sqlite_path: str = Field(
        default="",
        description="SQLite file for the local DocumentStore; empty means ~/.henchmen/henchmen_<environment>.db",
    )
    local_storage_dir: str = Field(
        default="",
        description="Directory for the local filesystem ObjectStore; empty means ~/.henchmen/storage",
    )
    local_serve_port: int = Field(default=8000, description="Port `henchmen serve` listens on")
    local_forward_base_url: str = Field(
        default="",
        description=(
            "Base URL operative containers use to deliver reports to the host in local mode. "
            "Empty means http://host.docker.internal:<local_serve_port>."
        ),
    )

    # Dispatch REST intake authentication
    dispatch_api_token: str = Field(
        default="",
        validation_alias=AliasChoices("HENCHMEN_DISPATCH_API_TOKEN", "DISPATCH_API_TOKEN"),
        description=(
            "Bearer token POST /api/v1/tasks requires (Authorization: Bearer <token>). Empty in DEV leaves the "
            "route open with a warning; empty in STAGING/PROD makes it return 401."
        ),
    )

    # Dispatch intake rate limiting (per client IP, per instance)
    dispatch_rate_limit_requests: int = Field(
        default=60,
        description="Maximum requests a single client IP may make to Dispatch intake routes per window.",
    )
    dispatch_rate_limit_window_seconds: float = Field(
        default=60.0,
        description="Length in seconds of the Dispatch rate-limit sliding window.",
    )
    dispatch_trust_forwarded_for: bool = Field(
        default=True,
        description=(
            "Key the Dispatch rate limiter on the left-most X-Forwarded-For entry. Keep true behind a trusted "
            "proxy (Cloud Run); set false when Dispatch is reachable directly, or callers can spoof their bucket."
        ),
    )

    # Observability
    metrics_auth_token: str = Field(
        default="",
        description=(
            "Bearer token required by the /metrics endpoints. Empty in DEV leaves them open with a warning; "
            "empty in STAGING/PROD makes them return 401."
        ),
    )

    # Lair (Cloud Run operative) defaults
    lair_default_cpu: str = Field(default="4", description="Default vCPU allocation for operative containers")
    lair_default_memory: str = Field(default="8Gi", description="Default memory allocation for operative containers")
    lair_default_timeout: int = Field(default=1800, description="Default operative timeout in seconds")
    lair_operative_image_tag: str = Field(default="latest", description="Operative container image tag or digest")
    lair_service_account: str = Field(
        default="",
        description=(
            "Service account email for operative Cloud Run Jobs. "
            "Empty means sa-<environment>-operative@<project>.iam.gserviceaccount.com."
        ),
    )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------

    @field_validator(*_SEEDED_SECRET_FIELDS, mode="after")
    @classmethod
    def _seeded_placeholder_is_unset(cls, value: str) -> str:
        """Treat Terraform's published placeholder secret as "not configured"."""
        return "" if value.strip() == SEEDED_SECRET_PLACEHOLDER else value

    def operative_env(self, *, include_secrets: bool = False) -> dict[str, str]:
        """``HENCHMEN_*`` variables to inject into an operative container.

        Empty values are omitted so container defaults apply. Secrets are only
        included when ``include_secrets`` is true (local Docker mode); in gcp
        mode they arrive through Secret Manager mounts under their bare names,
        which this class also accepts.
        """
        names = _OPERATIVE_ENV_FIELDS + (_OPERATIVE_SECRET_FIELDS if include_secrets else ())
        env: dict[str, str] = {}
        for name in names:
            value = getattr(self, name)
            if isinstance(value, StrEnum):
                value = value.value
            rendered = ("true" if value else "false") if isinstance(value, bool) else str(value)
            if rendered == "":
                continue
            env[f"HENCHMEN_{name.upper()}"] = rendered
        return env

    def validate_for_runtime(self) -> list[str]:
        """Problems that would break a real run, as human-readable messages.

        Returned rather than raised so ``henchmen doctor`` can show every
        problem at once and callers can decide whether to abort. Empty list
        means the configuration is coherent.
        """
        from henchmen.providers.tiers import active_llm_provider, tier_models

        problems: list[str] = []
        llm = active_llm_provider(self)

        if llm == "anthropic" and not self.anthropic_api_key:
            problems.append("HENCHMEN_ANTHROPIC_API_KEY is empty but the LLM provider is anthropic.")
        if llm == "openai" and not self.openai_api_key:
            problems.append("HENCHMEN_OPENAI_API_KEY is empty but the LLM provider is openai.")
        if llm == "gcp" and not self.gcp_project_id:
            problems.append("HENCHMEN_GCP_PROJECT_ID is empty but the LLM provider is Vertex AI.")
        if llm == "local" and not self.llm_ollama_base_url:
            problems.append("HENCHMEN_LLM_OLLAMA_BASE_URL is empty but the LLM provider is Ollama.")

        missing_tiers = [tier.value for tier, model in tier_models(self).items() if not model]
        if missing_tiers:
            problems.append(f"No model configured for LLM tier(s): {', '.join(sorted(missing_tiers))}.")

        if self.environment in (Environment.STAGING, Environment.PROD):
            if not self.pubsub_oidc_audience:
                problems.append(
                    f"HENCHMEN_PUBSUB_OIDC_AUDIENCE is required in {self.environment.value} "
                    "or every Pub/Sub push is rejected with 401."
                )
            if not self.metrics_auth_token:
                problems.append(
                    f"HENCHMEN_METRICS_AUTH_TOKEN is required in {self.environment.value} "
                    "or the /metrics endpoints return 401."
                )

        for float_field in ("operative_task_cost_ceiling_usd", "dispatch_rate_limit_window_seconds"):
            if float(getattr(self, float_field)) <= 0:
                problems.append(f"HENCHMEN_{float_field.upper()} must be greater than 0.")
        for field_name in (
            "dispatch_rate_limit_requests",
            "operative_wallclock_ceiling_seconds",
            "operative_max_output_tokens",
            "operative_max_system_tokens",
            "operative_max_message_tokens",
            "operative_heartbeat_interval_seconds",
            "local_serve_port",
        ):
            if int(getattr(self, field_name)) <= 0:
                problems.append(f"HENCHMEN_{field_name.upper()} must be greater than 0.")

        return problems

    @property
    def local_forward_base(self) -> str:
        """Where a local-mode operative container reaches the host `henchmen serve` process."""
        return self.local_forward_base_url or f"http://host.docker.internal:{self.local_serve_port}"

    @property
    def lair_service_account_email(self) -> str:
        """Operative job service account, defaulting to the Terraform-created per-environment SA."""
        if self.lair_service_account:
            return self.lair_service_account
        return f"sa-{self.environment.value}-operative@{self.gcp_project_id}.iam.gserviceaccount.com"


@lru_cache
def get_settings() -> Settings:
    """Return the cached application Settings singleton."""
    return Settings()
