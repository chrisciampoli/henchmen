"""LairManager - creates and monitors container jobs (Lairs) for Operative execution."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from henchmen.config.internal_auth import desktop_internal_auth
from henchmen.config.settings import DEFAULT_LOCAL_OPERATIVE_IMAGE
from henchmen.models.llm import ModelTier
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.models.scheme import SchemeNode
from henchmen.models.task import HenchmenTask
from henchmen.observability.tracker import TASK_EXECUTIONS_COLLECTION
from henchmen.providers.interfaces.container_orchestrator import ContainerOrchestrator, JobStatus
from henchmen.providers.interfaces.document_store import DocumentStore
from henchmen.providers.registry import orchestrator_is_local

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.models.dossier import Dossier

logger = logging.getLogger(__name__)

# Maximum number of entries to retain in each in-memory dict and TTL for cleanup.
_MAX_ENTRIES = 500
_ENTRY_TTL = timedelta(hours=2)

# Nodes that continue work on the operative's own feature branch rather than
# starting from the task's base branch.
_BRANCH_CONTINUATION_NODES = frozenset({"fix_lint", "fix_tests", "ci_fix"})

# Environment values are capped only to stay clear of the per-container
# environment size limits (Cloud Run: 32 KiB across all variables; `docker run
# -e` is bounded by the OS argument limit). Multi-kilobyte values are fine on
# both, and the fix nodes rely on the full lint/test output being present.
_MAX_TASK_DESCRIPTION_CHARS = 16_000
_MAX_TASK_TITLE_CHARS = 200

# Extra time allowed on top of the node timeout before the wait gives up: the
# container still has to start, upload its report and be observed as finished.
_WAIT_GRACE_SECONDS = 300

# After the job is observed as finished, how long to wait for its Pub/Sub
# report to land before falling back to the stores.
_REPORT_GRACE_SECONDS = 15


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


class LairManager:
    """Creates and monitors container jobs (Lairs) for Operative execution."""

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
        self,
        task: HenchmenTask,
        node: SchemeNode,
        lair_id: str,
        scheme_id: str = "",
        dossier: "Dossier | None" = None,
    ) -> dict[str, str]:
        """Build the environment variable dict for the operative container.

        Configuration travels as ``HENCHMEN_*`` variables produced by
        :meth:`Settings.operative_env`, so an operator override in
        ``.env.local`` (model tiers, token budgets, cost ceilings) reaches the
        container instead of silently falling back to the in-container
        defaults. On top of that sits the per-execution runtime contract
        (``TASK_ID``, ``NODE_ID``, ...), which is deliberately unprefixed
        because it is input, not configuration.
        """
        # Gated on the *effective* container orchestrator rather than the coarse
        # `provider` setting: an override (`HENCHMEN_CONTAINER_ORCHESTRATOR_PROVIDER`)
        # can point container orchestration at Docker, or away from it, independently
        # of `provider`. Everything below that only makes sense for a Lair actually
        # launched by Docker -- secrets in plain env vars, the host forward URL, the
        # Ollama rewrite, and the task token -- shares this one gate so they can never
        # disagree with each other.
        is_local = orchestrator_is_local(self.settings)
        env = self.settings.operative_env(include_secrets=is_local)

        env.update(
            {
                "TASK_ID": task.id,
                "NODE_ID": node.id,
                "SCHEME_ID": scheme_id,
                "LAIR_ID": lair_id,
                # Tier names resolve per provider inside the operative; a
                # concrete model name here would pin every provider to Gemini.
                "MODEL_NAME": node.model_name or ModelTier.COMPLEX.value,
                "REPO_URL": task.context.repo,
                # Fix/retry nodes must clone the feature branch (not the base
                # branch) so they can see and push to the operative's prior work.
                "BRANCH": (
                    task.branch_name if node.id in _BRANCH_CONTINUATION_NODES else (task.context.branch or "main")
                ),
                "TASK_TITLE": task.title[:_MAX_TASK_TITLE_CHARS],
                "TASK_DESCRIPTION": task.description[:_MAX_TASK_DESCRIPTION_CHARS],
            }
        )

        # The dossier is delivered out-of-band through the object store; only
        # advertise it once it has actually been serialized.
        artifact_uri = getattr(dossier, "artifact_uri", "") or ""
        if artifact_uri:
            env["DOSSIER_URI"] = artifact_uri

        if is_local:
            # The container talks back to the host `henchmen serve` process.
            env["HENCHMEN_LOCAL_FORWARD_BASE_URL"] = self.settings.local_forward_base
            ollama_url = env.get("HENCHMEN_LLM_OLLAMA_BASE_URL", "")
            if ollama_url:
                # Inside Docker, localhost is the container — use host.docker.internal
                for host in ("localhost", "127.0.0.1"):
                    ollama_url = ollama_url.replace(host, "host.docker.internal")
                env["HENCHMEN_LLM_OLLAMA_BASE_URL"] = ollama_url
            # Also expose the bare name for tooling (git, gh) that reads it directly.
            if self.settings.github_token:
                env["GITHUB_TOKEN"] = self.settings.github_token

            # Desktop install: a token valid only for this task authenticates the
            # operative's report and its task-state calls (D-P4). The internal push
            # token never enters an operative (amendment B2).
            internal = desktop_internal_auth()
            if internal is not None:
                env["HENCHMEN_OPERATIVE_TASK_TOKEN"] = internal.task_token(task.id)

        return env

    def _build_image(self) -> str:
        """Build the operative container image URI.

        Gated on the same effective-orchestrator predicate as the environment
        above: a Docker-launched lair always gets the local image, a Cloud Run
        lair always gets the Artifact Registry URI.
        """
        if orchestrator_is_local(self.settings):
            return self.settings.operative_image or DEFAULT_LOCAL_OPERATIVE_IMAGE
        return (
            f"{self.settings.gcp_region}-docker.pkg.dev/"
            f"{self.settings.gcp_project_id}/"
            f"henchmen-{self.settings.environment.value}/"
            f"operative:{self.settings.lair_operative_image_tag}"
        )

    async def _discard_stale_report(self, task_id: str, node_id: str) -> None:
        """Drop any report left over from a previous execution of this task/node.

        Without this, a re-execution (CI-fix loop, watchdog resume) consumes the
        *previous* attempt's report on its first poll and reports success before
        the new operative has done anything.
        """
        key = f"{task_id}:{node_id}"
        self._received_reports.pop(key, None)
        self._pending_reports.pop(key, None)
        try:
            await self._get_store().delete("operative_reports", key)
        except Exception as exc:
            logger.warning("Could not clear stale operative report for %s: %s", key, exc)

    async def create_lair(
        self,
        task: HenchmenTask,
        node: SchemeNode,
        scheme_id: str = "",
        dossier: "Dossier | None" = None,
    ) -> str:
        """Create and launch a container job for an operative. Returns lair_id."""
        self._cleanup_stale_entries()

        # Container job IDs: lowercase, digits, hyphens only, max 63 chars,
        # must start with a letter. The random suffix keeps a re-execution of
        # the same task/node (CI-fix loop, resume) from colliding with the
        # job resource created by the previous attempt.
        base_id = f"lair-{task.id[:8]}-{node.id}".replace("_", "-").lower()[:56]
        lair_id = f"{base_id}-{uuid4().hex[:6]}"

        cpu = self.settings.lair_default_cpu
        memory = self.settings.lair_default_memory
        timeout_seconds = node.timeout_seconds or self.settings.lair_default_timeout

        image = self._build_image()
        env_vars = self._build_env_vars(task, node, lair_id, scheme_id, dossier=dossier)

        # Local and AWS modes carry credentials in plain env vars; only Cloud Run
        # Jobs take a service account and Secret Manager references.
        if self.settings.provider == "gcp":
            service_account: str | None = self.settings.lair_service_account_email
            secrets: dict[str, str] | None = {
                "GITHUB_TOKEN": (
                    f"projects/{self.settings.gcp_project_id}/secrets/"
                    f"henchmen-{self.settings.environment.value}-github-token"
                ),
            }
        else:
            service_account = None
            secrets = None

        await self._discard_stale_report(task.id, node.id)

        logger.info("[LAIR] Creating lair %s for task %s node %s", lair_id, task.id, node.id)

        # Registered provisionally *before* the job starts: an operative that
        # finishes and reports before run_job returns must not be refused as
        # "never launched". Removed again if the launch itself fails.
        self._active_lairs[lair_id] = {
            "execution_id": "",
            "task_id": task.id,
            "node_id": node.id,
            "timeout_seconds": timeout_seconds,
            "created_at": datetime.now(UTC).isoformat(),
        }
        orchestrator = self._get_orchestrator()
        try:
            exec_id = await orchestrator.run_job(
                job_id=lair_id,
                image=image,
                env_vars=env_vars,
                cpu=cpu,
                memory=memory,
                timeout_seconds=timeout_seconds,
                service_account=service_account,
                secrets=secrets,
            )
        except BaseException:
            self._active_lairs.pop(lair_id, None)
            raise

        logger.info("[LAIR] Execution started: %s", exec_id)
        entry = self._active_lairs.get(lair_id)
        if entry is not None:
            entry["execution_id"] = exec_id
        else:
            # Evicted while run_job was awaited (a concurrent create_lair's stale-entry cleanup).
            logger.warning("[LAIR] Lair %s was evicted before its execution id could be recorded", lair_id)

        return lair_id

    def accepts_report_from(self, task_id: str, node_id: str, operative_id: str | None = None) -> bool:
        """True only when this manager launched a lair for ``task_id``/``node_id`` that is still reportable.

        Reuses the ``_active_lairs`` record ``create_lair`` writes (no second
        registry). ``operative_id``, when given, must be that lair's id — the
        operative reports ``LAIR_ID`` as its ``operative_id``. "Still
        reportable" is the window :meth:`wait_for_completion` itself waits: the
        node timeout plus the start-up and report grace periods from launch.
        Only the most recent lair for the task and node counts: a lair a
        re-execution superseded cannot report over its replacement. A lair
        launched by a previous process (a restart) is unknown here and refused:
        the in-flight scheme died with the process; desktop recovery is the
        Phase 3 poller's job.
        """
        launched: list[tuple[datetime, str, dict[str, Any]]] = []
        for lair_id, info in self._active_lairs.items():
            if info.get("task_id") != task_id or info.get("node_id") != node_id:
                continue
            created_at = _parse_iso(str(info.get("created_at", "")))
            if created_at is not None:
                launched.append((created_at, lair_id, info))
        if not launched:
            return False
        created_at, lair_id, info = max(launched, key=lambda entry: entry[0])
        if operative_id and operative_id != lair_id:
            return False
        timeout = int(info.get("timeout_seconds", self.settings.lair_default_timeout))
        window = timedelta(seconds=timeout + _WAIT_GRACE_SECONDS + _REPORT_GRACE_SECONDS)
        return created_at <= datetime.now(UTC) <= created_at + window

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

    async def _check_store_report(
        self, task_id: str, node_id: str, not_before: datetime | None = None
    ) -> OperativeReport | None:
        """Check DocumentStore for an operative report (cross-instance coordination).

        Reports older than *not_before* belong to an earlier execution of the
        same node and are ignored rather than consumed.
        """
        key = f"{task_id}:{node_id}"
        try:
            store = self._get_store()
            data = await store.get("operative_reports", key)
            if data is None:
                return None
            report = OperativeReport.model_validate(data)
        except Exception as exc:
            logger.warning("DocumentStore report check failed for %s: %s", key, exc)
            return None

        if not_before is not None:
            stamp = report.completed_at or report.started_at
            if stamp is not None and stamp < not_before:
                logger.warning("Ignoring stale operative report for %s (finished %s)", key, stamp.isoformat())
                return None

        # Consume it so a later execution of the same node cannot pick it up.
        try:
            await self._get_store().delete("operative_reports", key)
        except Exception as exc:
            logger.warning("Could not delete consumed operative report %s: %s", key, exc)
        return report

    async def _check_interrupted_report(
        self, task_id: str, node_id: str, not_before: datetime | None = None
    ) -> OperativeReport | None:
        """Pick up the partial INTERRUPTED report an operative persisted on SIGTERM.

        The operative writes it to ``task_executions/{task_id}.interrupted_report``
        *before* publishing, precisely so a publish killed by SIGKILL still
        leaves an authoritative record. Without reading it here the wait fell
        through to a fabricated FAILED report with zero telemetry, and the
        executor escalated instead of re-dispatching the interrupted node.
        """
        key = f"{task_id}:{node_id}"
        try:
            store = self._get_store()
            data = await store.get(TASK_EXECUTIONS_COLLECTION, task_id)
            if not data or data.get("interrupted_node_id") != node_id or not data.get("interrupted_report"):
                return None
            report = OperativeReport.model_validate(data["interrupted_report"])
        except Exception as exc:
            logger.warning("Interrupted report check failed for %s: %s", key, exc)
            return None

        if report.node_id != node_id:
            return None
        if not_before is not None:
            stamp = report.completed_at or report.started_at
            if stamp < not_before:
                return None

        # Consume it so a re-dispatch of the same node cannot pick it up again.
        try:
            await self._get_store().update(
                TASK_EXECUTIONS_COLLECTION,
                task_id,
                {"interrupted_node_id": None, "interrupted_report": None},
            )
        except Exception as exc:
            logger.warning("Could not clear consumed interrupted report for %s: %s", key, exc)
        logger.warning("[LAIR] Operative for %s was interrupted; using its partial report", key)
        return report

    def _fabricate_report(
        self,
        lair_id: str,
        task_id: str,
        node_id: str,
        status: OperativeStatus,
        summary: str,
    ) -> OperativeReport:
        """Build a synthetic report for a lair that never delivered one.

        Never COMPLETED: without a report there is no evidence the work was
        done or verified, and CLAUDE.md's fail-closed rule forbids promoting an
        unverified run to success.
        """
        now = datetime.now(UTC)
        return OperativeReport(
            task_id=task_id,
            scheme_id="",
            node_id=node_id,
            operative_id=lair_id,
            status=status,
            summary=summary,
            confidence_score=0.0,
            started_at=now,
            completed_at=now,
            error=summary,
        )

    async def wait_for_completion(
        self,
        lair_id: str,
        poll_interval: int = 10,
        timeout_seconds: int | None = None,
    ) -> OperativeReport:
        """Wait for the operative's real report via in-memory event, DocumentStore, or orchestrator polling.

        Three-tier approach for cross-instance reliability:
        1. In-memory event (same-instance fast path — Pub/Sub handler sets it directly)
        2. DocumentStore poll (cross-instance — operative_complete_handler writes report there)
        3. ContainerOrchestrator status (last resort — detects completion even if Pub/Sub lost)

        The wait is bounded by *timeout_seconds* (default: the node timeout
        recorded at ``create_lair`` plus a start-up/report grace period); on
        expiry the lair is cancelled and a TIMED_OUT report is returned.
        """
        lair_info = self._active_lairs.get(lair_id)
        if not lair_info:
            logger.error("[LAIR] wait_for_completion called for unknown lair %s", lair_id)
            return self._fabricate_report(
                lair_id, "", "", OperativeStatus.FAILED, f"Unknown lair {lair_id} — no execution to wait for"
            )

        task_id = str(lair_info.get("task_id", ""))
        node_id = str(lair_info.get("node_id", ""))
        key = f"{task_id}:{node_id}"
        created_at = _parse_iso(str(lair_info.get("created_at", "")))

        if timeout_seconds is None:
            timeout_seconds = int(lair_info.get("timeout_seconds", self.settings.lair_default_timeout))
            timeout_seconds += _WAIT_GRACE_SECONDS
        deadline = time.monotonic() + timeout_seconds

        # Check if report already arrived in-memory (Pub/Sub can be faster than our polling setup)
        if key in self._received_reports:
            report = self._received_reports.pop(key)
            self._pending_reports.pop(key, None)
            return report

        # Set up event for Pub/Sub notification (same-instance fast path)
        event = asyncio.Event()
        self._pending_reports[key] = event

        execution_id = str(lair_info.get("execution_id", ""))
        final_job_result = None
        timed_out = False

        # Poll: in-memory event, DocumentStore, and orchestrator status
        while True:
            # Tier 1: Check in-memory event (same instance)
            if event.is_set():
                break

            # Tier 2: Check DocumentStore (cross-instance)
            store_report = await self._check_store_report(task_id, node_id, not_before=created_at)
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
                        store_report = await self._check_store_report(task_id, node_id, not_before=created_at)
                        if store_report is not None:
                            self._pending_reports.pop(key, None)
                            return store_report
                        try:
                            await asyncio.wait_for(event.wait(), timeout=_REPORT_GRACE_SECONDS)
                        except TimeoutError:
                            logger.warning(
                                "Report not received for %s within %ss after execution finished",
                                key,
                                _REPORT_GRACE_SECONDS,
                            )
                        break
                except Exception as exc:
                    logger.warning("Failed to poll execution %s: %s", execution_id, exc)

            if time.monotonic() >= deadline:
                logger.error("[LAIR] Wait for %s exceeded %ds — cancelling lair", key, timeout_seconds)
                timed_out = True
                await self.cancel_lair(lair_id)
                break

            await asyncio.sleep(poll_interval)

        # Use real report if available from any source
        if key in self._received_reports:
            report = self._received_reports.pop(key)
            self._pending_reports.pop(key, None)
            return report

        # Final store check
        store_report = await self._check_store_report(task_id, node_id, not_before=created_at)
        if store_report is not None:
            self._pending_reports.pop(key, None)
            return store_report

        # The operative may have been interrupted and killed before its publish
        # went out; its partial report is still authoritative.
        interrupted_report = await self._check_interrupted_report(task_id, node_id, not_before=created_at)
        if interrupted_report is not None:
            self._pending_reports.pop(key, None)
            return interrupted_report

        # Fallback: fabricate a non-success report from the orchestrator status.
        self._pending_reports.pop(key, None)
        job_status = final_job_result.status if final_job_result is not None else None
        is_timeout = timed_out or job_status == JobStatus.TIMED_OUT
        status = OperativeStatus.TIMED_OUT if is_timeout else OperativeStatus.FAILED
        summary = f"Lair {lair_id} produced no report (job status: {job_status.value if job_status else 'unknown'})"
        logger.warning("[LAIR] %s — treating node as %s", summary, status.value)

        return self._fabricate_report(lair_id, task_id, node_id, status, summary)

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
