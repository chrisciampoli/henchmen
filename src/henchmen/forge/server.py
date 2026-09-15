"""Forge service - FastAPI Cloud Run service for CI/merge pipeline."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request

from henchmen.config.posture import fail_open_allowed
from henchmen.config.settings import get_settings
from henchmen.dispatch.pubsub_auth import require_internal_caller, verify_pubsub_oidc
from henchmen.utils.git import clone_repo
from henchmen.utils.redaction import install_secret_redaction

logger = logging.getLogger(__name__)

# Redact token-shaped secrets in every log record this process emits (clone
# URLs, CI output and GitHub errors can all carry one).
install_secret_redaction()

# Enough history for `git merge-base` against the PR base to resolve; the
# silent-failure scan and the changed-file lint both depend on it.
_CLONE_DEPTH = 200


class ForgeCIError(RuntimeError):
    """A CI run could not be completed.

    ``published`` records whether a failed forge-result has already been
    emitted for this request, so the Pub/Sub handler does not publish a second
    one. ``retriable`` is False for deterministic failures (an unparseable PR
    URL, a missing token) where redelivering the message cannot help.
    """

    def __init__(self, message: str, *, published: bool = False, retriable: bool = True) -> None:
        super().__init__(message)
        self.published = published
        self.retriable = retriable


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler for graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _sigterm_handler() -> None:
        logger.info("[forge] SIGTERM received, initiating graceful shutdown")

    # Windows does not support add_signal_handler.
    with contextlib.suppress(NotImplementedError):
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)

    from henchmen.observability.tracing import init_tracing, instrument_fastapi, shutdown_tracing
    from henchmen.providers.registry import ProviderRegistry

    settings = get_settings()
    for problem in settings.validate_for_runtime():
        logger.error("[forge] Configuration problem: %s", problem)

    init_tracing("forge", project_id=settings.gcp_project_id)
    instrument_fastapi(app)

    registry = ProviderRegistry(settings)
    # A broker injected on app.state beforehand belongs to whoever injected it;
    # one created here is closed and dropped on shutdown.
    owns_broker = getattr(app.state, "message_broker", None) is None
    _get_broker()
    app.state.ci_provider = registry.get_ci_provider()
    app.state.document_store = registry.get_document_store()

    logger.info("[forge] Service started")
    try:
        yield
    finally:
        # A sub-app entered after this one can fail to start; the combined app's
        # AsyncExitStack then unwinds this lifespan by throwing that exception in at
        # `yield`, so shutdown must run from `finally`, not after a bare `yield`.
        try:
            shutdown_tracing()
            logger.info("[forge] Shutting down")
            if owns_broker:
                await _close_broker()
        except Exception:
            # Never let a shutdown-path error mask the exception (if any) already
            # propagating through `yield`.
            logger.warning("[forge] Shutdown raised", exc_info=True)


app = FastAPI(title="Henchmen Forge", description="CI/merge pipeline", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/v1/process-queue", dependencies=[Depends(require_internal_caller)])
async def process_queue() -> dict[str, str | int]:
    """Merge-queue maintenance tick (called by Cloud Scheduler every 5 minutes).

    Henchmen never merges on an operative's behalf - every PR is merged by a
    human - so this tick deliberately does not drain the queue. It expires
    entries whose merge claim exceeded the TTL (a crashed claimant would
    otherwise block the queue forever) and reports the remaining depth.
    """
    from henchmen.forge.merge_queue import MergeQueue

    queue = MergeQueue(get_settings(), document_store=getattr(app.state, "document_store", None))
    expired = await queue.expire_stale_merging()
    pending = await queue.get_queue_length()
    logger.info("[FORGE] Merge queue tick: expired=%d pending=%d", expired, pending)
    return {"status": "ok", "processed": expired, "pending": pending}


@app.post("/pubsub/forge-request")
async def forge_request_handler(request: Request) -> dict[str, str]:
    """Pub/Sub push handler for CI requests.

    Receives a message with ``pr_url``, ``task_id``, and ``request_id``, then
    clones the PR branch, runs lint/tests/silent-failure checks, comments on the
    PR, and publishes the outcome to the forge-result topic before returning.
    """
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    message = envelope.get("message", {})
    data_b64 = message.get("data", "")
    try:
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode Pub/Sub message: {exc}") from exc

    pr_url = data.get("pr_url", "")
    task_id = data.get("task_id", "")
    # GCP push envelopes carry `message_id`; the local in-memory broker uses the
    # JSON-API spelling `messageId`. Accept both so local runs stay correlatable.
    request_id = data.get("request_id") or message.get("message_id") or message.get("messageId") or "unknown"

    if not pr_url or "pull/" not in pr_url:
        raise HTTPException(status_code=422, detail="Valid 'pr_url' is required in message data")

    # Run CI synchronously within the handler so the Pub/Sub ack is gated on completion.
    # This mirrors the Mastermind pattern: returning before completion would ack the Pub/Sub
    # message, causing lost CI results if the instance recycles mid-run. Cloud Run Pub/Sub
    # push tolerates long requests up to the subscription ack deadline.
    try:
        await _run_ci_for_pr(pr_url, task_id, request_id)
    except Exception as exc:
        logger.exception("[FORGE] CI run failed for pr=%s task=%s request=%s", pr_url, task_id, request_id)
        # Publish a failure result so Mastermind's pending-CI waiter is unblocked.
        if not getattr(exc, "published", False):
            try:
                await _publish_ci_failure(pr_url, task_id, request_id, reason="forge-exception")
            except Exception:
                logger.exception("[FORGE] Failed to publish CI failure notice for request=%s", request_id)
        if isinstance(exc, ForgeCIError) and not exc.retriable:
            # Deterministic failure: a redelivery would fail identically, and the
            # failed forge-result has already been published.
            return {"status": "failed", "reason": str(exc)}
        raise HTTPException(status_code=500, detail="CI run failed; Pub/Sub will retry") from None
    return {"status": "accepted"}


def _get_broker() -> Any:
    """Return the process-wide message broker, creating it on ``app.state`` once.

    The lifespan normally creates it; this covers an app whose lifespan did
    not run (e.g. mounted without one). A broker owns a Pub/Sub publisher
    client, so it is never built per message.
    """
    broker = getattr(app.state, "message_broker", None)
    if broker is None:
        from henchmen.providers.registry import ProviderRegistry

        broker = ProviderRegistry(get_settings()).get_message_broker()
        app.state.message_broker = broker
    return broker


async def _close_broker() -> None:
    """Release the lifespan's broker (e.g. its Pub/Sub publisher) and drop it from ``app.state``."""
    broker = getattr(app.state, "message_broker", None)
    app.state.message_broker = None
    aclose = getattr(broker, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception as exc:
        logger.warning("[forge] Failed to close message broker: %s", exc)


async def _publish_forge_result(payload: dict[str, Any], request_id: str) -> None:
    """Publish a forge-result message on the configured topic."""
    settings = get_settings()
    await _get_broker().publish(
        settings.pubsub_topic_forge_result,
        json.dumps(payload).encode("utf-8"),
        request_id=request_id,
    )


async def _publish_ci_failure(pr_url: str, task_id: str, request_id: str, reason: str) -> None:
    """Publish a forge-result failure so Mastermind's pending-CI waiter is unblocked."""
    await _publish_forge_result(
        {
            "pr_url": pr_url,
            "task_id": task_id,
            "request_id": request_id,
            "status": "failed",
            "reason": reason,
        },
        request_id,
    )


async def _fail(
    pr_url: str,
    task_id: str,
    request_id: str,
    reason: str,
    detail: str,
    *,
    retriable: bool,
) -> ForgeCIError:
    """Publish a failed forge-result and build the matching ForgeCIError."""
    try:
        await _publish_ci_failure(pr_url, task_id, request_id, reason=reason)
        published = True
    except Exception:
        logger.exception("[FORGE] Failed to publish CI failure (%s) for request=%s", reason, request_id)
        published = False
    return ForgeCIError(f"{reason}: {detail}", published=published, retriable=retriable)


def _parse_pr_url(pr_url: str) -> tuple[str, int]:
    """Parse ``https://github.com/owner/repo/pull/N`` into ``(owner/repo, N)``."""
    parts = pr_url.rstrip("/").split("/")
    owner = parts[3]
    repo_name = parts[4]
    pr_number = int(parts[6])
    return f"{owner}/{repo_name}", pr_number


async def _run_ci_for_pr(pr_url: str, task_id: str, request_id: str) -> None:
    """Clone the PR branch, run CI checks, comment on the PR, and publish results.

    Every failure path publishes a failed forge-result before raising, so a task
    can never sit in ``ci_pending`` because Forge gave up quietly.
    """
    settings = get_settings()

    try:
        full_repo, pr_number = _parse_pr_url(pr_url)
    except (IndexError, ValueError) as exc:
        logger.error("[FORGE] Cannot parse PR URL %s: %s", pr_url, exc)
        raise await _fail(pr_url, task_id, request_id, "parse-error", str(exc), retriable=False) from exc

    # HENCHMEN_GITHUB_TOKEN or the bare GITHUB_TOKEN, via Settings' AliasChoices.
    github_token = settings.github_token
    if not github_token and not fail_open_allowed(settings):
        raise await _fail(
            pr_url,
            task_id,
            request_id,
            "missing-github-token",
            "HENCHMEN_GITHUB_TOKEN (or GITHUB_TOKEN) is not set; refusing to run CI unauthenticated.",
            retriable=False,
        )

    workspace = tempfile.mkdtemp(prefix="forge-ci-")
    try:
        # --- Get PR metadata from GitHub -----------------------------------
        try:
            from github import Auth, Github

            client = Github(auth=Auth.Token(github_token)) if github_token else Github()
            github_repo = client.get_repo(full_repo)
            pr = github_repo.get_pull(pr_number)
            head_branch = pr.head.ref
            base_branch = pr.base.ref
        except Exception as exc:
            logger.error("[FORGE] GitHub lookup failed for %s: %s", pr_url, exc)
            raise await _fail(pr_url, task_id, request_id, "github-api-error", str(exc), retriable=True) from exc

        # --- Clone the repo (shallow, single branch) ----------------------
        try:
            await clone_repo(
                full_repo,
                head_branch,
                workspace,
                token=github_token or None,
                depth=_CLONE_DEPTH,
            )
        except RuntimeError as exc:
            logger.error("[FORGE] %s", exc)
            raise await _fail(pr_url, task_id, request_id, "clone-failed", str(exc), retriable=True) from exc

        # --- Run CI checks -------------------------------------------------
        from henchmen.forge.ci_runner import CIRunner

        try:
            # One budget for the whole run and for any single command: the run
            # must finish (and ack) inside the 600s Pub/Sub ack deadline.
            budget = settings.forge_ci_timeout_seconds
            runner = CIRunner(
                timeout_seconds=budget,
                total_budget_seconds=budget,
                redact=[github_token] if github_token else [],
            )
            result = await runner.run(workspace, base_ref=base_branch)
        except Exception as exc:
            raise await _fail(pr_url, task_id, request_id, "ci-error", str(exc), retriable=True) from exc

        # --- Comment on the PR ---------------------------------------------
        try:
            pr.create_issue_comment(_build_comment(result, task_id))
        except Exception as exc:
            logger.warning("[FORGE] Failed to comment on PR: %s", exc)

        # --- Publish result to forge-result topic ---------------------------
        await _publish_forge_result(
            {
                "pr_url": pr_url,
                "task_id": task_id,
                "request_id": request_id,
                "status": _result_status(result),
                "summary": result.get("summary", ""),
                "skipped": result.get("skipped", []),
                "failed": result.get("failed", []),
            },
            request_id,
        )

        logger.info(
            "[FORGE] CI %s for %s (skipped=%s)",
            _result_status(result).upper(),
            pr_url,
            ",".join(result.get("skipped", [])) or "none",
        )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


def _result_status(result: dict[str, Any]) -> str:
    """Map a CIRunner result to the forge-result ``status`` field.

    ``passed`` only when every check ran and passed. A run whose only problem
    is a skipped check is ``incomplete`` - the PR was not verified, so it must
    never be recorded as a CI pass (Mastermind treats anything other than
    ``passed`` as not passed).
    """
    if result.get("passed"):
        return "passed"
    if result.get("incomplete") and not result.get("failed"):
        return "incomplete"
    return "failed"


def _build_comment(result: dict[str, Any], task_id: str) -> str:
    """Render the CI result as a PR comment.

    Skipped checks are called out explicitly: a check that never ran must not
    read as a green tick.
    """
    skipped = result.get("skipped", [])
    status = _result_status(result)
    headline = status.upper()
    if status == "incomplete":
        headline = f"INCOMPLETE ({len(skipped)} check(s) skipped - this PR was not fully verified)"
    status_emoji = {"passed": "white_check_mark", "incomplete": "warning"}.get(status, "x")

    body = f"## Henchmen CI Results :{status_emoji}:\n\n**Status:** {headline}\n**Task:** `{task_id}`\n\n"
    emoji_by_status = {"passed": "white_check_mark", "failed": "x", "skipped": "warning"}
    for check in result.get("checks", []):
        check_emoji = emoji_by_status.get(check.get("status", ""), "grey_question")
        body += f"### :{check_emoji}: {check['name']} — {check.get('status', 'unknown')}\n"
        detail = check.get("output") or check.get("error") or ""
        if check.get("status") == "skipped":
            detail = check.get("error") or detail
        if detail:
            body += f"```\n{detail[:2000]}\n```\n"
    return body


@app.post("/pubsub/build-complete")
async def build_complete_handler(request: Request) -> dict[str, str]:
    """Cloud Build completion callback (Pub/Sub push, OIDC-authenticated)."""
    await verify_pubsub_oidc(request, get_settings())
    try:
        envelope = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {exc}") from exc

    message = envelope.get("message", {})
    data_b64 = message.get("data", "")
    try:
        data = json.loads(base64.b64decode(data_b64).decode("utf-8"))
    except Exception:
        logger.exception("Failed to decode build-complete Pub/Sub message data")
        data = {}

    build_id = data.get("id", "unknown")
    status = data.get("status", "unknown")
    logger.info("Build complete callback: build_id=%s status=%s", build_id, status)
    return {"status": "ok", "build_id": build_id, "build_status": status}
