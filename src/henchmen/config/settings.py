"""Global application configuration using pydantic-settings."""

from enum import StrEnum
from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    STAGING = "staging"
    PROD = "prod"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HENCHMEN_",
        env_file=(".env.local", ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
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
    llm_provider: str = Field(default="", description="Override LLM provider (gcp, aws, local, openai, anthropic)")
    ci_provider: str = Field(default="", description="Override CI provider")

    # Pub/Sub topics (defaults include environment prefix)
    pubsub_topic_task_intake: str = Field(default="")
    pubsub_topic_task_planned: str = Field(default="")
    pubsub_topic_operative_dispatch: str = Field(default="")
    pubsub_topic_operative_status: str = Field(default="")
    pubsub_topic_operative_complete: str = Field(default="")
    pubsub_topic_forge_request: str = Field(default="")
    pubsub_topic_forge_result: str = Field(default="")
    pubsub_topic_dead_letter: str = Field(default="")
    pubsub_topic_embed_request: str = Field(default="")
    pubsub_topic_ci_failure: str = Field(default="")

    def model_post_init(self, __context: object) -> None:
        """Set environment-prefixed defaults for Pub/Sub topics and validate provider requirements."""
        if self.provider == "gcp" and not self.gcp_project_id:
            msg = (
                "HENCHMEN_GCP_PROJECT_ID is required when HENCHMEN_PROVIDER=gcp. "
                "Set it in your .env.local or environment."
            )
            raise ValueError(msg)

        env = self.environment.value
        defaults = {
            "pubsub_topic_task_intake": f"henchmen-{env}-task-intake",
            "pubsub_topic_task_planned": f"henchmen-{env}-task-planned",
            "pubsub_topic_operative_dispatch": f"henchmen-{env}-operative-dispatch",
            "pubsub_topic_operative_status": f"henchmen-{env}-operative-status",
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
    gcs_bucket_tfstate: str = Field(default="", description="GCS bucket for Terraform state")
    gcs_bucket_snapshots: str = Field(default="", description="GCS bucket for operative snapshots")

    # Arsenal MCP server
    arsenal_mcp_server_url: str = Field(default="http://localhost:8080", description="Arsenal MCP server URL")

    # Git identity for operative commits
    git_author_email: str = Field(
        default="henchmen-operative@noreply.local", description="Git author email for operative commits"
    )
    git_author_name: str = Field(default="Henchmen Operative", description="Git author name for operative commits")

    # GitHub integration
    github_app_id: str = Field(default="", description="GitHub App ID")
    github_app_private_key_secret: str = Field(
        default="", description="Secret Manager resource name for GitHub App private key"
    )
    github_webhook_secret: str = Field(default="", description="GitHub webhook secret")
    github_token: str = Field(default="", description="GitHub personal access token for operative clone/push")
    github_default_org: str = Field(default="", description="Default GitHub organization")
    github_default_repo: str = Field(default="", description="Default target repo for tasks")

    # Slack integration
    slack_bot_token_secret: str = Field(default="", description="Secret Manager resource name for Slack bot token")
    slack_signing_secret: str = Field(default="", description="Slack signing secret")
    slack_app_token_secret: str = Field(
        default="", description="Secret Manager resource name for Slack app-level token"
    )
    slack_notification_channel: str = Field(default="", description="Default Slack channel for notifications")

    # Jira integration
    jira_base_url: str = Field(default="", description="Jira instance base URL")
    jira_email: str = Field(default="", description="Jira service account email")
    jira_api_token_secret: str = Field(default="", description="Secret Manager resource name for Jira API token")
    jira_project_key: str = Field(default="", description="Default Jira project key")
    jira_webhook_secret: str = Field(
        default="", description="Shared secret for Jira webhook HMAC verification (X-Atlassian-Webhook-Signature)"
    )

    # Vertex AI model names
    vertex_ai_model_complex: str = Field(default="gemini-2.5-pro", description="Model for complex reasoning tasks")
    vertex_ai_model_light: str = Field(default="gemini-2.5-pro", description="Model for lightweight tasks")

    # Operative context limits (token-based)
    operative_max_system_tokens: int = Field(default=20_000, description="Max tokens for system prompt")
    operative_max_message_tokens: int = Field(default=16_000, description="Max tokens for a single message")

    # Operative cost ceilings (L5 fix: task-level ceiling spans all nodes)
    operative_task_cost_ceiling_usd: float = Field(
        default=6.0,
        description="Maximum cumulative cost in USD for a single task across all scheme nodes.",
    )
    operative_wallclock_ceiling_seconds: int = Field(
        default=1800,
        description="Wall-clock ceiling (seconds) used as a cost proxy for free local providers (e.g. Ollama).",
    )

    # Operative liveness (K5 fix: intra-node heartbeat)
    operative_heartbeat_interval_seconds: int = Field(
        default=60,
        description="Interval between intra-node heartbeat writes from the operative to Firestore.",
    )

    # Pub/Sub push authentication (A6 fix: in-app OIDC verification)
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

    # Vertex AI context caching
    vertex_ai_context_cache_enabled: bool = Field(default=True, description="Enable Gemini context caching")
    vertex_ai_context_cache_min_tokens: int = Field(
        default=32_768, description="Minimum tokens required to create a cache"
    )

    # Vertex AI safety settings
    vertex_ai_safety_threshold: str = Field(
        default="BLOCK_MEDIUM_AND_ABOVE", description="Safety filter threshold for Gemini"
    )

    # Vertex AI evaluation
    vertex_ai_evaluation_enabled: bool = Field(default=False, description="Enable post-operative GenAI evaluation")

    # Vertex AI grounding
    vertex_ai_grounding_enabled: bool = Field(default=True, description="Enable Google Search grounding")

    # Vertex AI experiments
    vertex_ai_experiments_enabled: bool = Field(default=False, description="Enable Vertex AI Experiments tracking")
    vertex_ai_experiment_name: str = Field(default="henchmen-operatives", description="Vertex AI experiment name")

    # Vertex AI RAG Engine (replaces Pinecone)
    rag_corpus_display_name: str = Field(default="henchmen-code", description="RAG corpus display name")
    rag_corpus_region: str = Field(
        default="us-west1", description="GCP region for RAG Engine corpus (may differ from main region)"
    )
    rag_embedding_model: str = Field(default="text-embedding-005", description="Embedding model for RAG corpus")

    # Ollama (local LLM)
    llm_ollama_base_url: str = Field(default="http://localhost:11434", description="Ollama server URL")
    llm_ollama_model: str = Field(default="llama3.2", description="Default Ollama model")
    llm_ollama_skip_probe: bool = Field(
        default=False,
        description=(
            "Skip the Ollama tool-calling capability probe issued on the first "
            "generate() call with tools. Set to True in CI or when running with "
            "mocked httpx clients that don't mimic a real Ollama server."
        ),
    )

    # AWS settings (used when provider=aws)
    aws_region: str = Field(default="us-east-1", description="AWS region")
    aws_account_id: str = Field(default="", description="AWS account ID")
    aws_resource_prefix: str = Field(default="henchmen", description="Prefix for AWS resource names")
    aws_dynamodb_table: str = Field(default="henchmen", description="DynamoDB table name")
    aws_ecs_cluster: str = Field(default="henchmen", description="ECS cluster name")
    aws_ecs_subnets: str = Field(default="", description="Comma-separated subnet IDs for ECS tasks")
    aws_ecs_security_groups: str = Field(default="", description="Comma-separated security group IDs")

    # Direct API keys (used when llm_provider=openai or anthropic)
    openai_api_key: str = Field(default="", description="OpenAI API key")
    anthropic_api_key: str = Field(default="", description="Anthropic API key")

    # OpenAI model tier mapping (L10 fix — avoid hard-coded model names in providers)
    openai_model_complex: str = Field(default="gpt-4.1", description="OpenAI model used for the COMPLEX tier")
    openai_model_light: str = Field(default="gpt-4.1-mini", description="OpenAI model used for the LIGHT tier")
    openai_model_reasoning: str = Field(default="o3", description="OpenAI model used for the REASONING tier")

    # Anthropic model tier mapping (L10 fix — avoid hard-coded model names in providers)
    anthropic_model_complex: str = Field(
        default="claude-sonnet-4-6-20250514",
        description="Anthropic model used for the COMPLEX tier",
    )
    anthropic_model_light: str = Field(
        default="claude-haiku-4-5-20251001",
        description="Anthropic model used for the LIGHT tier",
    )
    anthropic_model_reasoning: str = Field(
        default="claude-opus-4-6-20250514",
        description="Anthropic model used for the REASONING tier",
    )

    # Lair (Cloud Run operative) defaults
    lair_default_cpu: str = Field(default="4", description="Default vCPU allocation for operative containers")
    lair_default_memory: str = Field(default="8Gi", description="Default memory allocation for operative containers")
    lair_default_timeout: int = Field(default=1800, description="Default operative timeout in seconds")
    lair_operative_image_tag: str = Field(default="latest", description="Operative container image tag or digest")


@lru_cache
def get_settings() -> Settings:
    """Return the cached application Settings singleton."""
    return Settings()
