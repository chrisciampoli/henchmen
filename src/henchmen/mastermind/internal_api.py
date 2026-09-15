"""Task-scoped routes a desktop install's operatives use instead of the data volume.

Operative containers must never mount ``/data`` (it holds every secret), yet
they need three pieces of task state: the task's running cost (to seed the
task cost ceiling), a liveness heartbeat, and a partial report when SIGTERM
interrupts them. These routes expose exactly those operations, each gated by
the task-scoped token the Lair injected (:mod:`henchmen.config.internal_auth`),
so an operative can touch only its own task. They exist only on a desktop
install; elsewhere they answer 404.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, ValidationError

from henchmen.dispatch.pubsub_auth import (
    MAX_OPERATIVE_REPORT_BYTES,
    _load_desktop_internal_auth,
    _read_capped_body,
    _split_bearer,
)
from henchmen.models.operative import OperativeReport, OperativeStatus
from henchmen.observability.tracker import TERMINAL_EXECUTION_STATES

logger = logging.getLogger(__name__)

INTERNAL_TASKS_PREFIX = "/internal/tasks"
_TASK_EXECUTIONS_COLLECTION = "task_executions"
# pydantic-core's regex engine has no look-around support, so "." and ".."
# cannot be excluded by the pattern itself; both are rejected explicitly in
# require_task_token instead (neither is ever a real task id).
_TASK_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"
_RESERVED_TASK_IDS = frozenset({".", ".."})
# How far into the future a report's own timestamps may drift from the
# server's clock before it is refused (ordinary clock skew, not a forged report).
_CLOCK_SKEW_TOLERANCE_SECONDS = 60


class TaskCost(BaseModel):
    """The only field of the task document an operative may read."""

    estimated_cost_usd: float = Field(..., description="Cost recorded by earlier nodes of this task, in USD")


async def require_task_token(task_id: Annotated[str, PathParam(pattern=_TASK_ID_PATTERN)], request: Request) -> None:
    """Allow the request only with the token derived for exactly this ``task_id``.

    Reusable: any router whose paths contain ``{task_id}`` can declare
    ``dependencies=[Depends(require_task_token)]`` (Plan 2B's
    ``POST /mastermind/internal/tasks/{task_id}/github-token`` does).

    404 outside a desktop install (via :func:`_load_desktop_internal_auth`,
    which also turns a secrets-directory ``OSError`` into a 503 rather than
    letting it surface as a raw 500); 401 (with ``WWW-Authenticate: Bearer``)
    unless the bearer equals ``InternalAuth.task_token(task_id)`` for this
    path's ``task_id`` (constant-time via ``tokens_match``). The internal
    push token is never accepted here -- only a token derived for this exact
    task authenticates these routes. ``task_id`` is rejected as malformed
    (422) before any of that -- including a bare ``.`` or ``..``, which the
    pattern alone cannot exclude since pydantic-core's regex engine has no
    look-around support.
    """
    if task_id in _RESERVED_TASK_IDS:
        raise HTTPException(status_code=422, detail="Invalid task id")
    internal = _load_desktop_internal_auth()
    if internal is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not internal.verify_task_token(task_id, _split_bearer(request.headers.get("Authorization"))):
        logger.warning(
            "[internal-api] Refusing task-scoped request for task %s without a valid task token from %s",
            task_id,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=401,
            detail="Missing or invalid operative task token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def task_store() -> Any:
    """The shared Mastermind document store, for task-scoped internal routes.

    Public accessor (controller ruling C1): every route in this module -- and
    every route Plan 2B adds under ``INTERNAL_TASKS_PREFIX`` -- reaches the
    document store through this one function, never a second lookup path.
    """
    # Imported lazily: server.py includes this router at import time.
    from henchmen.mastermind import server

    return server.get_agent().tracker._store


async def _load_active_task_document(task_id: str) -> dict[str, Any]:
    """Fetch the task document a heartbeat or interrupted-report write is about to touch.

    Neither route may create a document -- unlike ``DocumentStore.update``'s
    normal "create when missing" semantics -- and neither may write onto a
    task Mastermind has already finished (``execution_state`` in
    :data:`~henchmen.observability.tracker.TERMINAL_EXECUTION_STATES`):
    404 when there is no execution record yet, 409 when the task is done.
    """
    document: dict[str, Any] | None = await task_store().get(_TASK_EXECUTIONS_COLLECTION, task_id)
    if document is None:
        raise HTTPException(status_code=404, detail="No execution record for this task")
    if document.get("execution_state") in TERMINAL_EXECUTION_STATES:
        raise HTTPException(status_code=409, detail="Task has already finished")
    return document


router = APIRouter(prefix=INTERNAL_TASKS_PREFIX, dependencies=[Depends(require_task_token)], include_in_schema=False)


@router.get("/{task_id}/cost")
async def get_task_cost(task_id: str) -> TaskCost:
    """Seed for the operative's task cost ceiling."""
    document = await task_store().get(_TASK_EXECUTIONS_COLLECTION, task_id)
    if document is None:
        raise HTTPException(status_code=404, detail="No execution record for this task")
    return TaskCost(estimated_cost_usd=float(document.get("estimated_cost_usd", 0.0) or 0.0))


@router.post("/{task_id}/heartbeat", status_code=204)
async def post_heartbeat(task_id: str) -> Response:
    """Record liveness; the time is the server's, never the operative's."""
    await _load_active_task_document(task_id)
    await task_store().update(_TASK_EXECUTIONS_COLLECTION, task_id, {"last_heartbeat": datetime.now(UTC).isoformat()})
    return Response(status_code=204)


@router.put("/{task_id}/interrupted-report", status_code=204)
async def put_interrupted_report(task_id: str, request: Request) -> Response:
    """Persist the partial report an operative writes when SIGTERM interrupts it.

    The body is read through the same capped reader Task 6 uses for
    ``/pubsub/operative-complete``, so an oversized body is rejected while
    streaming rather than after being fully buffered and parsed.
    """
    await _read_capped_body(request, MAX_OPERATIVE_REPORT_BYTES)
    try:
        report = OperativeReport.model_validate_json(await request.body())
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Invalid OperativeReport body") from exc
    if report.task_id != task_id:
        raise HTTPException(status_code=422, detail="The report belongs to another task")
    if report.status != OperativeStatus.INTERRUPTED:
        raise HTTPException(status_code=422, detail="Only an interrupted report can be stored here")
    skew_limit = datetime.now(UTC) + timedelta(seconds=_CLOCK_SKEW_TOLERANCE_SECONDS)
    if report.started_at > skew_limit or (report.completed_at is not None and report.completed_at > skew_limit):
        raise HTTPException(status_code=422, detail="Report timestamps are too far in the future")
    await _load_active_task_document(task_id)
    await task_store().update(
        _TASK_EXECUTIONS_COLLECTION,
        task_id,
        {
            "interrupted_node_id": report.node_id,
            "interrupted_at": datetime.now(UTC).isoformat(),
            "interrupted_report": report.model_dump(mode="json"),
            "execution_state": "interrupted",
        },
    )
    return Response(status_code=204)
