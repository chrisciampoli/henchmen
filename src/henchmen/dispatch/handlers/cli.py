"""CLI / REST task creation handler.

Dispatch only normalizes and publishes. Repository (re)indexing lives in
:mod:`henchmen.dossier.embed_pipeline` (``henchmen embed`` and Mastermind's
embed-request consumer).
"""

from typing import TYPE_CHECKING, Any

from henchmen.dispatch.api_models import CreateTaskRequest
from henchmen.dispatch.normalizer import TaskNormalizer
from henchmen.providers.interfaces.message_broker import MessageBroker

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


async def handle_cli_request(
    data: CreateTaskRequest,
    normalizer: TaskNormalizer,
    settings: "Settings",
    broker: MessageBroker,
) -> dict[str, Any]:
    """Process a CLI task creation request."""
    task = normalizer.from_cli(data.model_dump(), settings)
    msg_id = await normalizer.publish_task(task, settings, broker=broker)
    return {"task_id": task.id, "message_id": msg_id, "status": "dispatched"}
