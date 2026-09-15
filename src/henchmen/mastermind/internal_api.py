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

from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi import Path as PathParam
from pydantic import BaseModel, Field, ValidationError

from henchmen.config.internal_auth import desktop_internal_auth
from henchmen.dispatch.pubsub_auth import MAX_OPERATIVE_REPORT_BYTES, _read_capped_body, _split_bearer
from henchmen.models.operative import OperativeReport, OperativeStatus

INTERNAL_TASKS_PREFIX = "/internal/tasks"
_TASK_EXECUTIONS_COLLECTION = "task_executions"
_TASK_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,128}$"


class TaskCost(BaseModel):
    """The only field of the task document an operative may read."""

    estimated_cost_usd: float = Field(..., description="Cost recorded by earlier nodes of this task, in USD")


async def require_task_token(task_id: Annotated[str, PathParam(pattern=_TASK_ID_PATTERN)], request: Request) -> None:
    """Allow the request only with the token derived for exactly this ``task_id``.

    Reusable: any router whose paths contain ``{task_id}`` can declare
    ``dependencies=[Depends(require_task_token)]`` (Plan 2B's
    ``POST /mastermind/internal/tasks/{task_id}/github-token`` does).

    404 outside a desktop install; 401 (with ``WWW-Authenticate: Bearer``)
    unless the bearer equals ``InternalAuth.task_token(task_id)`` for this
    path's ``task_id`` (constant-time via ``tokens_match``). The internal
    push token is never accepted here -- only a token derived for this exact
    task authenticates these routes.
    """
    internal = desktop_internal_auth()
    if internal is None:
        raise HTTPException(status_code=404, detail="Not Found")
    if not internal.verify_task_token(task_id, _split_bearer(request.headers.get("Authorization"))):
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
