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
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException, Request

from henchmen.config.posture import fail_open_allowed
from henchmen.config.settings import get_settings
from henchmen.dispatch.pubsub_auth import require_internal_caller, verify_pubsub_oidc
from henchmen.providers.local.memory import FORWARD_TIMEOUT_SECONDS
from henchmen.providers.registry import orchestrator_is_local
from henchmen.utils.git import clone_repo
from henchmen.utils.github_auth import MAX_MIN_TTL_SECONDS, GitHubAuthError, get_github_token_async
from henchmen.utils.lifespan import run_shutdown
from henchmen.utils.message_dedup import claim_message, mark_message_done, release_message_claim
from henchmen.utils.redaction import install_secret_redaction

if TYPE_CHECKING:
    from henchmen.config.settings import Settings
    from henchmen.forge.ci_runner import CIRunner

logger = logging.getLogger(__name__)

# Redact token-shaped secrets in every log record this process emits (clone
# URLs, CI output and GitHub errors can all carry one).
install_secret_redaction()

# Enough history for `git merge-base` against the PR base to resolve; the
# silent-failure scan and the changed-file lint both depend on it.
_CLONE_DEPTH = 200

# On a desktop install this handler runs inline inside the shared in-memory
# broker's forwarded POST, which re-sends the request after
# FORWARD_TIMEOUT_SECONDS. The whole CI run must end strictly before that, with
# this much headroom left for the GitHub calls, the clone, the PR comment and
# publishing the result.
_DESKTOP_FORWARD_HEADROOM_SECONDS = 120

# Dedup keys for forge-request deliveries share the processed_messages
# collection with Mastermind's markers on a desktop install; the prefix keeps
# them apart.
_FORGE_REQUEST_DEDUP_PREFIX = "forge-request:"


def desktop_ci_budget_seconds(settings: Settings) -> int:
    """The single wall-clock budget of one desktop Forge CI run: the gate timeout, capped below the broker re-send."""
    return int(min(float(settings.lair_default_timeout), FORWARD_TIMEOUT_SECONDS - _DESKTOP_FORWARD_HEADROOM_SECONDS))


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

    async def _shutdown() -> None:
        shutdown_tracing()
        logger.info("[forge] Shutting down")
        if owns_broker:
            await _close_broker()

    original: BaseException | None = None
    try:
        yield
    except BaseException as exc:
        original = exc
        raise
    finally:
        # A sub-app entered after this one can fail to start; the combined app's
        # AsyncExitStack then unwinds this lifespan by throwing that exception in at
        # `yield`, so shutdown must run from `finally`, not after a bare `yield`, and a
        # shutdown-path error (a CancelledError included) never masks `original`.
        await run_shutdown("forge", _shutdown, original=original)


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

    # Desktop (local orchestrator): the shared in-memory broker re-sends a forwarded
    # POST after FORWARD_TIMEOUT_SECONDS. Claim the request id first (the same
    # two-phase markers Mastermind uses) so a re-send of a still-running or
    # finished request never runs CI -- and posts a PR comment -- a second time.
    # The cloud path is unchanged.
    dedup_key = ""
    if orchestrator_is_local(get_settings()) and request_id != "unknown":
        dedup_key = f"{_FORGE_REQUEST_DEDUP_PREFIX}{request_id}"
        try:
            duplicate = await claim_message(
                _get_document_store(), dedup_key, handler="forge-request", ttl_seconds=FORWARD_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.error("[FORGE] Could not claim forge-request %s: %s", request_id, exc)
            raise HTTPException(status_code=500, detail="Could not record the CI request") from None
        if duplicate:
            logger.info("[FORGE] Duplicate forge-request %s for %s; not running CI again", request_id, pr_url)
            return {"status": "duplicate", "request_id": request_id}

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
            if dedup_key:
                await mark_message_done(_get_document_store(), dedup_key, handler="forge-request")
            return {"status": "failed", "reason": str(exc)}
        if dedup_key:
            await release_message_claim(_get_document_store(), dedup_key)
        raise HTTPException(status_code=500, detail="CI run failed; Pub/Sub will retry") from None
    if dedup_key:
        await mark_message_done(_get_document_store(), dedup_key, handler="forge-request")
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


def _get_document_store() -> Any:
    """Return the process-wide document store on ``app.state``, creating it once like :func:`_get_broker`."""
    store = getattr(app.state, "document_store", None)
    if store is None:
        from henchmen.providers.registry import ProviderRegistry

        store = ProviderRegistry(get_settings()).get_document_store()
        app.state.document_store = store
    return store


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
    started = asyncio.get_running_loop().time()

    try:
        full_repo, pr_number = _parse_pr_url(pr_url)
    except (IndexError, ValueError) as exc:
        logger.error("[FORGE] Cannot parse PR URL %s: %s", pr_url, exc)
        raise await _fail(pr_url, task_id, request_id, "parse-error", str(exc), retriable=False) from exc

    # Desktop (effective container orchestrator is local): the PR's code is
    # operative-written and must never run in this server process. Only the
    # silent-failure scan (git plumbing plus text analysis, no checkout)
    # stays here; lint and tests run in the gate container (decision C18).
    local = orchestrator_is_local(settings)
    # One budget for the whole run and for any single command: the cloud
    # run must finish (and ack) inside the 600s Pub/Sub ack deadline. A
    # desktop run gets the gate timeout, capped below the in-memory broker's
    # forward re-send (desktop_ci_budget_seconds), counted from the start of
    # this request.
    budget = desktop_ci_budget_seconds(settings) if local else settings.forge_ci_timeout_seconds

    # An installation token scoped to this repository when a GitHub App is configured, else the PAT.
    # It clones at the start and (on desktop) goes into the gate, so it must outlive the whole run.
    min_ttl = int(budget) + _DESKTOP_FORWARD_HEADROOM_SECONDS
    if min_ttl > MAX_MIN_TTL_SECONDS:
        logger.warning(
            "[FORGE] The CI budget (%ss) is longer than a GitHub installation token is guaranteed to last (%ss); "
            "a GitHub App token may expire before CI finishes",
            int(budget),
            MAX_MIN_TTL_SECONDS,
        )
    try:
        github_token = await get_github_token_async(full_repo, settings=settings, min_ttl_seconds=min_ttl)
    except GitHubAuthError as exc:
        logger.error("[FORGE] GitHub credentials unavailable for %s: %s", pr_url, exc)
        raise await _fail(pr_url, task_id, request_id, "github-credentials", str(exc), retriable=True) from exc
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
                no_checkout=local,
            )
        except RuntimeError as exc:
            logger.error("[FORGE] %s", exc)
            raise await _fail(pr_url, task_id, request_id, "clone-failed", str(exc), retriable=True) from exc

        # --- Run CI checks -------------------------------------------------
        from henchmen.forge.ci_runner import CIRunner

        try:
            runner = CIRunner(
                timeout_seconds=budget,
                total_budget_seconds=budget,
                redact=[github_token] if github_token else [],
            )
            if local:
                result = await _run_local_ci(
                    settings,
                    runner,
                    full_repo,
                    head_branch,
                    base_branch,
                    workspace,
                    token=github_token,
                    deadline=started + budget,
                )
            else:
                result = await runner.run(workspace, base_ref=base_branch)
        except Exception as exc:
            raise await _fail(pr_url, task_id, request_id, "ci-error", str(exc), retriable=True) from exc

        # --- Comment on the PR ---------------------------------------------
        # A fresh token and client: the run can outlast the token the lookup used (ruling PI-15).
        try:
            comment_token = await get_github_token_async(full_repo, settings=settings)
            comment_client = Github(auth=Auth.Token(comment_token)) if comment_token else Github()
            comment_pr = comment_client.get_repo(full_repo).get_pull(pr_number)
            comment_pr.create_issue_comment(_build_comment(result, task_id))
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


async def _run_local_ci(
    settings: Settings,
    runner: CIRunner,
    full_repo: str,
    head_branch: str,
    base_branch: str,
    workspace: str,
    *,
    token: str,
    deadline: float,
) -> dict[str, Any]:
    """Desktop Forge CI: one gate container for lint and tests, the silent-failure scan on the no-checkout clone.

    ``ci_gate forge`` clones once, installs dependencies once (as the
    unprivileged user) and reports both checks in one result, through the
    single container runner (``run_gate_in_container``) with the gate timeout.
    Lint is scoped by :mod:`~henchmen.mastermind.scheme_executor.lint_scope`
    (the Mastermind lint gate's rules), which is stricter than the cloud host
    path's ruff-on-changed-``.py``-files.

    Whether there is a tests check at all -- and whether it is ``skipped`` for a
    ``package.json`` without a ``test`` script -- is decided from the committed
    tree exactly as the host path decides it
    (:meth:`CIRunner.committed_tests_decision`). The results are combined by
    :meth:`CIRunner.aggregate`, so the forge-result status mapping
    (``passed``/``failed``/``incomplete``) and the PR comment are those of the
    host path. A gate that times out or reports no usable result fails every
    check it was asked to run.

    ``deadline`` (event-loop time) is the one wall-clock limit of the whole
    run: the gate gets what is left of it, the silent-failure scan whatever the
    gate left over, and a scan with nothing left is ``skipped`` (so the run is
    at best ``incomplete``, never ``passed``).

    ``token`` is the credentials provider's token for ``full_repo``, fetched by
    the caller to outlive the whole budget; it reaches the gate over stdin only.
    """
    from henchmen.forge.ci_runner import STATUS_FAILED, STATUS_PASSED, STATUS_SKIPPED
    from henchmen.mastermind.scheme_executor.handlers import run_gate_in_container

    loop = asyncio.get_running_loop()
    run_tests, tests_check = await runner.committed_tests_decision(workspace)
    names = ["lint", "tests"] if run_tests else ["lint"]
    remaining = deadline - loop.time()
    if remaining <= 0:
        gate: dict[str, Any] = {"condition": "fail", "message": "forge failed (no CI time budget left)", "output": ""}
    else:
        gate = await run_gate_in_container(
            settings,
            "forge",
            repo=full_repo,
            branch=head_branch,
            base_branch=base_branch,
            token=token,
            extra_args=() if run_tests else ("--skip-tests",),
            timeout_seconds=remaining,
        )
    reported = {str(check.get("name")): check for check in gate.get("checks", []) if isinstance(check, dict)}
    gate_failed = gate.get("condition") != "pass"
    any_check_failed = any(check.get("condition") != "pass" for check in reported.values())
    checks: list[dict[str, Any]] = []
    for name in names:
        check = reported.get(name)
        if check is None or (gate_failed and not any_check_failed):
            # No per-check result (a timeout, a crash, no marker) or a pass the exit code contradicts.
            message = f"{name}: {gate.get('message') or 'the gate reported no result'}"
            checks.append(runner.check_result(name, STATUS_FAILED, str(gate.get("output", "")), message))
            continue
        passed = check.get("condition") == "pass"
        checks.append(
            runner.check_result(
                name,
                STATUS_PASSED if passed else STATUS_FAILED,
                str(check.get("output", "")),
                "" if passed else str(check.get("message", "")),
            )
        )
    if tests_check is not None:
        checks.append(tests_check)
    scan_budget = int(deadline - loop.time())
    if scan_budget <= 0:
        checks.append(
            runner.check_result(
                "silent_failure_scan", STATUS_SKIPPED, "", "The CI time budget was used up before the scan could run."
            )
        )
    else:
        runner.timeout_seconds = min(runner.timeout_seconds, scan_budget)
        runner.total_budget_seconds = scan_budget
        checks.append(await runner.run_silent_failure_scan(workspace, base_branch))
    return runner.aggregate(checks)


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
