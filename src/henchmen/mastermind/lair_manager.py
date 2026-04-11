"""LairManager - creates and monitors Cloud Run Jobs (Lairs) for Operative execution."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.providers.interfaces.container_orchestrator import ContainerOrchestrator, JobStatus
from henchmen.providers.interfaces.document_store import DocumentStore

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# Maximum number of entries to retain in each in-memory dict and TTL for cleanup.
_MAX_ENTRIES = 500
_ENTRY_TTL = timedelta(hours=2)


class LairManager:
    """Creates and monitors Cloud Run Jobs (Lairs) for Operative execution."""

    def __init__(
        self,
        settings: "Settings",
        container_orchestrator: ContainerOrchestrator | None = None,
        document_store: DocumentStore | None = None,
    ) -> None:
        self.settings = settings
        self._active_lairs: dict[str, dict[str, Any]] = {}  # lair_id -> job info
        self._pending_reports: dict[str, asyncio.Event] = {}  # task_id:node_id -> event
        self._received_reports: dict[str, OperativeReport] = {}  # task_id:node_id -> report

        # Providers — resolved lazily if not injected
        self._orchestrator = container_orchestrator
        self._store = document_store

    def _get_orchestrator(self) -> ContainerOrchestrator:
        """Lazy-init ContainerOrchestrator from GCP Cloud Run."""
        if self._orchestrator is None:
            from henchmen.providers.gcp.cloud_run import CloudRunOrchestrator

            self._orchestrator = CloudRunOrchestrator(self.settings)
        return self._orchestrator

    def _get_store(self) -> DocumentStore:
        """Lazy-init DocumentStore from GCP Firestore."""
        if self._store is None:
            from henchmen.providers.gcp.firestore import FirestoreDocumentStore

            self._store = FirestoreDocumentStore(self.settings)
        return self._store

    def _cleanup_stale_entries(self) -> None:
        """Remove stale entries from in-memory dicts to prevent unbounded growth.

        Evicts entries older than _ENTRY_TTL, and if still over _MAX_ENTRIES,
        removes the oldest entries by creation time.
        """
        cutoff = (datetime.now(UTC) - _ENTRY_TTL).isoformat()

        # Clean _active_lairs by created_at timestamp
        if len(self._active_lairs) > _MAX_ENTRIES // 2:
            stale_ids = [lid for lid, info in self._active_lairs.items() if info.get("created_at", "") < cutoff]
            for lid in stale_ids:
                self._active_lairs.pop(lid, None)
            if stale_ids:
                logger.info("[lair-cleanup] Evicted %d stale active_lairs entries", len(stale_ids))

        # Bound _pending_reports by size
        if len(self._pending_reports) > _MAX_ENTRIES:
            excess = len(self._pending_reports) - _MAX_ENTRIES
            keys_to_remove = list(self._pending_reports.keys())[:excess]
            for k in keys_to_remove:
                self._pending_reports.pop(k, None)
            logger.info("[lair-cleanup] Evicted %d entries from pending_reports", excess)

        # Bound _received_reports by size
        if len(self._received_reports) > _MAX_ENTRIES:
            excess = len(self._received_reports) - _MAX_ENTRIES
            keys_to_remove = list(self._received_reports.keys())[:excess]
            for k in keys_to_remove:
                self._received_reports.pop(k, None)
            logger.info("[lair-cleanup] Evicted %d entries from received_reports", excess)

    def _build_env_vars(
        self, task: HenchmenTask, node: SchemeNode, lair_id: str, scheme_id: str = ""
    ) -> dict[str, str]:
        """Build plain environment variable dict for the operative container."""
        import os

        env = {
            "TASK_ID": task.id,
            "NODE_ID": node.id,
            "SCHEME_ID": scheme_id,
            "LAIR_ID": lair_id,
            "MODEL_NAME": node.model_name or self.settings.vertex_ai_model_complex,
            "REPO_URL": task.context.repo,
            # Fix/retry nodes must clone the feature branch (not main)
            # so they can see and push to the operative's prior work.
            "BRANCH": (task.branch_name if node.id in ("fix_lint", "fix_tests") else (task.context.branch or "main")),
            "TASK_TITLE": task.title[:200],
            "TASK_DESCRIPTION": task.description[:500],
            "HENCHMEN_GCP_PROJECT_ID": self.settings.gcp_project_id,
            "HENCHMEN_ENVIRONMENT": self.settings.environment.value,
            "HENCHMEN_GCP_REGION": self.settings.gcp_region,
        }

        # Local mode: pass LLM config and GitHub token as plain env vars.
        # In GCP mode these come from Secret Manager and Vertex AI settings.
        if self.settings.provider == "local":
            llm_provider = self.settings.llm_provider or "local"
            env.update(
                {
                    "HENCHMEN_PROVIDER": "local",
                    "HENCHMEN_LLM_PROVIDER": llm_provider,
                    "HENCHMEN_GIT_AUTHOR_NAME": self.settings.git_author_name,
                    "HENCHMEN_GIT_AUTHOR_EMAIL": self.settings.git_author_email,
                }
            )
            # Ollama: rewrite URL so the container can reach the host
            if llm_provider == "local":
                ollama_url = self.settings.llm_ollama_base_url
                # Inside Docker, localhost is the container — use host.docker.internal
                ollama_url = ollama_url.replace("localhost", "host.docker.internal")
                ollama_url = ollama_url.replace("127.0.0.1", "host.docker.internal")
                env["HENCHMEN_LLM_OLLAMA_BASE_URL"] = ollama_url
                env["HENCHMEN_LLM_OLLAMA_MODEL"] = self.settings.llm_ollama_model
            elif llm_provider == "openai":
                env["HENCHMEN_OPENAI_API_KEY"] = self.settings.openai_api_key
            elif llm_provider == "anthropic":
                env["HENCHMEN_ANTHROPIC_API_KEY"] = self.settings.anthropic_api_key

            # GitHub token from environment (not Secret Manager in local mode)
            github_token = os.environ.get("GITHUB_TOKEN", "")
            if github_token:
                env["GITHUB_TOKEN"] = github_token

        return env

    def _build_image(self) -> str:
        """Build the operative container image URI."""
        if self.settings.provider == "local":
            return "henchmen-operative:local"
        return (
            f"{self.settings.gcp_region}-docker.pkg.dev/"
            f"{self.settings.gcp_project_id}/"
            f"henchmen-{self.settings.environment.value}/"
            f"operative:{self.settings.lair_operative_image_tag}"
        )

    async def create_lair(self, task: HenchmenTask, node: SchemeNode, scheme_id: str = "") -> str:
        """Create and launch a container job for an operative. Returns lair_id."""
        self._cleanup_stale_entries()

        # Cloud Run Job IDs: lowercase, digits, hyphens only, max 63 chars, must start with letter
        raw_id = f"lair-{task.id[:8]}-{node.id}"
        lair_id = raw_id.replace("_", "-").lower()[:63]

        # Scale up resources for long-running nodes
        if node.timeout_seconds > 300:
            cpu = "4"
            memory = "8Gi"
        else:
            cpu = self.settings.lair_default_cpu
            memory = self.settings.lair_default_memory

        image = self._build_image()
        env_vars = self._build_env_vars(task, node, lair_id, scheme_id)

        # Local mode: GITHUB_TOKEN is already in env_vars (plain), no Secret Manager.
        if self.settings.provider == "local":
            service_account = None
            secrets = None
        else:
            service_account = f"sa-dev-operative@{self.settings.gcp_project_id}.iam.gserviceaccount.com"
            secrets = {
                "GITHUB_TOKEN": (
                    f"projects/{self.settings.gcp_project_id}/secrets/"
                    f"henchmen-{self.settings.environment.value}-github-token"
                ),
            }

        logger.info("[LAIR] Creating lair %s for task %s node %s", lair_id, task.id, node.id)

        orchestrator = self._get_orchestrator()
        exec_id = await orchestrator.run_job(
            job_id=lair_id,
            image=image,
            env_vars=env_vars,
            cpu=cpu,
            memory=memory,
            timeout_seconds=node.timeout_seconds,
            service_account=service_account,
            secrets=secrets,
        )

        logger.info("[LAIR] Execution started: %s", exec_id)

        self._active_lairs[lair_id] = {
            "execution_id": exec_id,
            "task_id": task.id,
            "node_id": node.id,
            "created_at": datetime.now(UTC).isoformat(),
        }

        return lair_id

    def notify_operative_complete(self, report: OperativeReport) -> None:
        """Called by the Pub/Sub handler when an operative-complete message arrives.

        Stores the real OperativeReport (with tokens, cost, files_changed) and
        signals the waiting wait_for_completion() to pick it up.
        """
        key = f"{report.task_id}:{report.node_id}"
        self._received_reports[key] = report
        event = self._pending_reports.get(key)
        if event:
            event.set()
            logger.info(
                "Operative report received for %s (tokens: %d in, %d out)",
                key,
                report.total_input_tokens,
                report.total_output_tokens,
            )
        else:
            logger.info("Operative report received for %s (no waiter yet, stored for pickup)", key)

    async def _check_store_report(self, task_id: str, node_id: str) -> OperativeReport | None:
        """Check DocumentStore for an operative report (cross-instance coordination)."""
        try:
            store = self._get_store()
            key = f"{task_id}:{node_id}"
            data = await store.get("operative_reports", key)
            if data is not None:
                return OperativeReport.model_validate(data)
        except Exception as exc:
            logger.warning("DocumentStore report check failed for %s:%s: %s", task_id, node_id, exc)
        return None

    async def wait_for_completion(self, lair_id: str, poll_interval: int = 10) -> OperativeReport:
        """Wait for the operative's real report via in-memory event, DocumentStore, or orchestrator polling.

        Three-tier approach for cross-instance reliability:
        1. In-memory event (same-instance fast path — Pub/Sub handler sets it directly)
        2. DocumentStore poll (cross-instance — operative_complete_handler writes report there)
        3. ContainerOrchestrator status (last resort — detects completion even if Pub/Sub lost)
        """
        lair_info = self._active_lairs.get(lair_id, {})
        task_id = lair_info.get("task_id", "")
        node_id = lair_info.get("node_id", "")
        key = f"{task_id}:{node_id}"

        # Check if report already arrived in-memory (Pub/Sub can be faster than our polling setup)
        if key in self._received_reports:
            report = self._received_reports.pop(key)
            self._pending_reports.pop(key, None)
            return report

        # Set up event for Pub/Sub notification (same-instance fast path)
        event = asyncio.Event()
        self._pending_reports[key] = event

        execution_id = lair_info.get("execution_id", "")
        final_job_result = None

        # Poll: in-memory event, DocumentStore, and orchestrator status
        while True:
            # Tier 1: Check in-memory event (same instance)
            if event.is_set():
                break

            # Tier 2: Check DocumentStore (cross-instance)
            store_report = await self._check_store_report(task_id, node_id)
            if store_report is not None:
                self._pending_reports.pop(key, None)
                logger.info("Retrieved operative report from DocumentStore for %s", key)
                return store_report

            # Tier 3: Poll orchestrator status
            if execution_id:
                try:
                    orchestrator = self._get_orchestrator()
                    job_result = await orchestrator.get_status(execution_id)
                    if job_result.status not in (JobStatus.PROVISIONING, JobStatus.RUNNING):
                        final_job_result = job_result
                        # Execution finished — check store one more time, then wait briefly for in-memory
                        store_report = await self._check_store_report(task_id, node_id)
                        if store_report is not None:
                            self._pending_reports.pop(key, None)
                            return store_report
                        try:
                            await asyncio.wait_for(event.wait(), timeout=15)
                        except TimeoutError:
                            logger.warning("Report not received for %s within 15s after execution finished", key)
                        break
                except Exception as exc:
                    logger.warning("Failed to poll execution %s: %s", execution_id, exc)

            await asyncio.sleep(poll_interval)

        # Use real report if available from any source
        if key in self._received_reports:
            report = self._received_reports.pop(key)
            self._pending_reports.pop(key, None)
            return report

        # Final store check
        store_report = await self._check_store_report(task_id, node_id)
        if store_report is not None:
            self._pending_reports.pop(key, None)
            return store_report

        # Fallback: fabricate report from orchestrator status
        self._pending_reports.pop(key, None)
        succeeded = final_job_result is not None and final_job_result.status == JobStatus.COMPLETED
        status = OperativeStatus.COMPLETED if succeeded else OperativeStatus.FAILED
        logger.warning("Using fabricated report for %s (report not received from any source)", key)

        return OperativeReport(
            task_id=task_id,
            scheme_id="",
            node_id=node_id,
            operative_id=lair_id,
            status=status,
            summary=f"Lair {lair_id} finished with status {status.value}",
            confidence_score=1.0 if succeeded else 0.0,
            started_at=datetime.now(UTC),
            completed_at=datetime.now(UTC),
        )

    async def cancel_lair(self, lair_id: str) -> None:
        """Cancel a running Lair."""
        lair_info = self._active_lairs.get(lair_id)
        if not lair_info:
            logger.warning("Cannot cancel unknown lair: %s", lair_id)
            return

        execution_id = lair_info.get("execution_id", "")
        try:
            orchestrator = self._get_orchestrator()
            await orchestrator.cancel(execution_id)
            logger.info("Cancelled lair %s", lair_id)
        except Exception as exc:
            logger.error("Failed to cancel lair %s: %s", lair_id, exc)

    async def get_lair_status(self, lair_id: str) -> dict[str, Any]:
        """Get current status of a Lair."""
        lair_info = self._active_lairs.get(lair_id)
        if not lair_info:
            return {"status": "unknown", "lair_id": lair_id}

        return {
            "status": "active",
            "lair_id": lair_id,
            **lair_info,
        }
