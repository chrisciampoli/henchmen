"""Forge service - FastAPI Cloud Run service for CI/merge pipeline."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import shutil
import signal
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request

from henchmen.config.settings import get_settings

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan handler for graceful shutdown."""
    loop = asyncio.get_running_loop()

    def _sigterm_handler() -> None:
        logger.info("[forge] SIGTERM received, initiating graceful shutdown")

    try:
        loop.add_signal_handler(signal.SIGTERM, _sigterm_handler)
    except NotImplementedError:
        pass  # Windows doesn't support add_signal_handler

    from henchmen.observability.tracing import init_tracing, instrument_fastapi, shutdown_tracing
    from henchmen.providers.registry import ProviderRegistry

    settings = get_settings()
    init_tracing("forge", project_id=settings.gcp_project_id)
    instrument_fastapi(app)

    registry = ProviderRegistry(settings)
    app.state.message_broker = registry.get_message_broker()
    app.state.ci_provider = registry.get_ci_provider()
    app.state.document_store = registry.get_document_store()

    logger.info("[forge] Service started")
    yield
    shutdown_tracing()
    logger.info("[forge] Shutting down")


app = FastAPI(title="Henchmen Forge", description="CI/merge pipeline", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check endpoint."""
    return {"status": "ok"}


@app.post("/api/v1/process-queue")
async def process_queue() -> dict[str, str | int]:
    """Process the merge queue (called by Cloud Scheduler every 5 minutes)."""
    logger.info("[FORGE] Merge queue processing triggered")
    return {"status": "ok", "processed": 0}


@app.post("/pubsub/forge-request")
async def forge_request_handler(request: Request) -> dict[str, str]:
    """Pub/Sub push handler for CI requests.

    Receives a message with ``pr_url``, ``task_id``, and ``request_id``.
    Kicks off a background task that clones the PR branch, runs lint/tests,
    comments on the PR, and publishes results to the ``forge-result`` topic.
    """
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
    request_id = data.get("request_id", message.get("message_id", "unknown"))

    if not pr_url or "pull/" not in pr_url:
        raise HTTPException(status_code=422, detail="Valid 'pr_url' is required in message data")

    # Run CI in the background so we can ACK the Pub/Sub push immediately.
    asyncio.create_task(_run_ci_for_pr(pr_url, task_id, request_id))
    return {"status": "accepted"}


async def _run_ci_for_pr(pr_url: str, task_id: str, request_id: str) -> None:
    """Clone the PR branch, run CI checks, comment on the PR, and publish results."""
    settings = get_settings()

    # Parse PR URL: https://github.com/owner/repo/pull/N
    parts = pr_url.rstrip("/").split("/")
    try:
        owner = parts[3]
        repo_name = parts[4]
        pr_number = int(parts[6])
    except (IndexError, ValueError) as exc:
        logger.error("[FORGE] Cannot parse PR URL %s: %s", pr_url, exc)
        return

    full_repo = f"{owner}/{repo_name}"
    github_token = os.environ.get("GITHUB_TOKEN", "")

    workspace = tempfile.mkdtemp(prefix="forge-ci-")
    try:
        # --- Get PR metadata from GitHub -----------------------------------
        from github import Github

        g = Github(github_token) if github_token else Github()
        github_repo = g.get_repo(full_repo)
        pr = github_repo.get_pull(pr_number)
        head_branch = pr.head.ref

        # --- Clone the repo (shallow, single branch) ----------------------
        if github_token:
            clone_url = f"https://x-access-token:{github_token}@github.com/{full_repo}.git"
        else:
            clone_url = f"https://github.com/{full_repo}.git"

        proc = await asyncio.create_subprocess_exec(
            "git",
            "clone",
            "--depth=1",
            "--branch",
            head_branch,
            clone_url,
            workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        clone_stdout, clone_stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "[FORGE] git clone failed (rc=%d): %s",
                proc.returncode,
                clone_stderr.decode(errors="replace")[:500],
            )
            return

        # --- Run CI checks -------------------------------------------------
        from henchmen.forge.ci_runner import CIRunner

        runner = CIRunner()
        result = await runner.run(workspace)

        # --- Comment on the PR ---------------------------------------------
        status_emoji = "white_check_mark" if result["passed"] else "x"
        comment_body = (
            f"## Henchmen CI Results :{status_emoji}:\n\n"
            f"**Status:** {'PASSED' if result['passed'] else 'FAILED'}\n"
            f"**Task:** `{task_id}`\n\n"
        )
        for check in result.get("checks", []):
            check_emoji = "white_check_mark" if check["passed"] else "x"
            comment_body += f"### :{check_emoji}: {check['name']}\n"
            if check.get("output"):
                comment_body += f"```\n{check['output'][:2000]}\n```\n"

        try:
            pr.create_issue_comment(comment_body)
        except Exception as exc:
            logger.warning("[FORGE] Failed to comment on PR: %s", exc)

        # --- Publish result to forge-result topic ---------------------------
        try:
            result_data = json.dumps(
                {
                    "pr_url": pr_url,
                    "task_id": task_id,
                    "request_id": request_id,
                    "status": "passed" if result["passed"] else "failed",
                    "summary": result.get("summary", ""),
                }
            ).encode("utf-8")
            await app.state.message_broker.publish(
                settings.pubsub_topic_forge_result,
                result_data,
                request_id=request_id,
            )
        except Exception as exc:
            logger.warning("[FORGE] Failed to publish result: %s", exc)

        logger.info("[FORGE] CI %s for %s", "PASSED" if result["passed"] else "FAILED", pr_url)

    except Exception as exc:
        logger.exception("[FORGE] CI error for %s: %s", pr_url, exc)
    finally:
        shutil.rmtree(workspace, ignore_errors=True)


@app.post("/pubsub/build-complete")
async def build_complete_handler(request: Request) -> dict[str, str]:
    """Cloud Build completion callback."""
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
