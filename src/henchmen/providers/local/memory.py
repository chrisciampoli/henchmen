"""In-memory MessageBroker for local development.

Two deployment shapes use this broker:

* ``henchmen serve`` runs Dispatch, Mastermind and Forge in one process. A
  single shared instance (see :func:`set_shared_broker`) forwards each
  publish as an HTTP POST to the mounted sub-application, simulating Pub/Sub
  push delivery. The topic-to-URL map is :func:`default_forward_map`.
* A local operative container is a *separate* process. It cannot share the
  host's instance, so when ``HENCHMEN_LOCAL_FORWARD_BASE_URL`` is set in its
  environment the broker forwards to the host (normally
  ``http://host.docker.internal:<port>``). Without this the operative's
  completion report would never leave the container.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from collections import defaultdict, deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from henchmen.config.settings import Settings

logger = logging.getLogger(__name__)

# In local mode the "push subscription" handler runs the whole scheme inline,
# so a forwarded POST legitimately stays open for as long as an operative run.
_FORWARD_TIMEOUT_SECONDS = 1800.0
# Transient transport failures (the target service still starting up) are
# retried; an HTTP response — even a 5xx — is not, the handler already saw it.
_FORWARD_RETRIES = 3
_FORWARD_RETRY_BACKOFF_SECONDS = 0.5
# Published messages are kept only for test inspection; cap them so a
# long-running `henchmen serve` does not grow without bound.
_MESSAGE_HISTORY = 1000

# Module-level singleton for single-process mode. When set, all calls to
# InMemoryMessageBroker() return this instance so Dispatch, Mastermind,
# and Forge share the same broker (and its forward map).
_shared_instance: InMemoryMessageBroker | None = None


def set_shared_broker(broker: InMemoryMessageBroker) -> None:
    """Designate *broker* as the process-wide singleton."""
    global _shared_instance
    _shared_instance = broker


def get_shared_broker() -> InMemoryMessageBroker | None:
    """Return the shared broker, or None if not in singleton mode."""
    return _shared_instance


def default_forward_map(settings: Settings, base_url: str) -> dict[str, str]:
    """Canonical topic -> push-endpoint map for local mode.

    ``base_url`` is where the single-process server is reachable from the
    publisher: ``http://localhost:<port>`` inside ``henchmen serve`` itself,
    ``http://host.docker.internal:<port>`` from an operative container.
    """
    base = base_url.rstrip("/")
    return {
        settings.pubsub_topic_task_intake: f"{base}/mastermind/pubsub/task-intake",
        settings.pubsub_topic_operative_complete: f"{base}/mastermind/pubsub/operative-complete",
        settings.pubsub_topic_forge_request: f"{base}/forge/pubsub/forge-request",
        settings.pubsub_topic_forge_result: f"{base}/mastermind/pubsub/forge-result",
        settings.pubsub_topic_ci_failure: f"{base}/mastermind/pubsub/ci-failure",
    }


class InMemoryMessageBroker:
    """MessageBroker backed by in-process async queues.

    Optionally forwards publishes as HTTP POSTs to simulate Pub/Sub push
    delivery when running all services in a single process, or to reach the
    host process from an operative container.
    """

    def __new__(cls, settings: Settings | None = None) -> InMemoryMessageBroker:
        if _shared_instance is not None:
            return _shared_instance
        return super().__new__(cls)

    def __init__(self, settings: Settings | None = None) -> None:
        if hasattr(self, "_initialized"):
            return
        self._initialized = True
        self._messages: dict[str, deque[dict[str, Any]]] = defaultdict(lambda: deque(maxlen=_MESSAGE_HISTORY))
        self._subscribers: dict[str, list[Callable[..., Any]]] = defaultdict(list)
        self._forward_map: dict[str, str] = {}
        self._forward_token: str | None = None
        # Strong references to in-flight forward tasks. Without this, the asyncio
        # event loop only holds weak references and background tasks can be
        # garbage collected mid-run (silent message loss in local dev).
        self._background_tasks: set[asyncio.Task[bool]] = set()
        # Operative containers on a desktop install authenticate with their task token.
        if settings is not None and settings.operative_task_token.strip():
            self._forward_token = settings.operative_task_token.strip()
        # Operative containers: forward to the host when explicitly configured.
        if settings is not None and settings.local_forward_base_url:
            self.set_forward_map(default_forward_map(settings, settings.local_forward_base_url))
            logger.info("[broker] Forwarding local publishes to %s", settings.local_forward_base_url)

    async def drain(self) -> None:
        """Wait for all in-flight forward tasks to complete.

        Call this from an application lifespan shutdown hook to avoid losing
        in-transit messages on clean shutdown.
        """
        if not self._background_tasks:
            return
        await asyncio.gather(*self._background_tasks, return_exceptions=True)

    def set_forward_map(self, mapping: dict[str, str]) -> None:
        """Set topic -> URL mapping for HTTP forwarding.

        When a message is published to a topic in the map, an HTTP POST is
        sent to the URL with a Pub/Sub-style envelope. This simulates
        Pub/Sub push subscriptions for local development.
        """
        self._forward_map = mapping

    def set_forward_token(self, token: str | None) -> None:
        """Bearer token sent with every forwarded POST (desktop installs authenticate internal pushes)."""
        self._forward_token = token or None

    def _record_publish(self, topic: str, data: bytes, attributes: dict[str, str]) -> str:
        """Append to message history and invoke synchronous subscribers. Returns the local message id."""
        msg_id = f"local-{uuid4().hex[:8]}"
        self._messages[topic].append({"id": msg_id, "data": data, "attributes": attributes})
        for callback in self._subscribers.get(topic, []):
            callback(data, **attributes)
        return msg_id

    async def publish(self, topic: str, data: bytes, ordering_key: str | None = None, **attributes: str) -> str:
        """Publish a message to the given topic. Returns a local message ID.

        Non-blocking and best-effort: the server-side shared broker inside
        ``henchmen serve`` must never block a caller on a slow or failing
        forward, so any HTTP delivery happens in a background task whose
        outcome this method does not wait for. A caller that must know
        whether delivery actually succeeded (an operative container
        confirming its own completion report) uses :meth:`publish_and_confirm`
        instead.
        """
        msg_id = self._record_publish(topic, data, attributes)

        # HTTP forwarding (non-blocking, best-effort). Hold a strong reference
        # to the task and clean up via a done-callback to prevent GC from reaping
        # the in-flight forward before it completes.
        url = self._forward_map.get(topic)
        if url:
            task = asyncio.create_task(self._forward_to_http(url, msg_id, data, attributes))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return msg_id

    def has_forward_target(self, topic: str) -> bool:
        """True when a forward URL is configured for ``topic``.

        An operative container has one; so does the shared server-side broker
        forwarding to itself inside ``henchmen serve``.
        """
        return topic in self._forward_map

    async def publish_and_confirm(self, topic: str, data: bytes, **attributes: str) -> bool:
        """Publish and await actual HTTP delivery. True only on a 2xx response from the forward URL.

        Unlike :meth:`publish`, this awaits :meth:`_forward_to_http` directly
        instead of scheduling a background task — so a caller (the operative
        process reporting its own completion) does not exit before delivery
        is confirmed one way or the other. Returns False when no forward URL
        is configured for ``topic`` — there is nothing to confirm.
        """
        msg_id = self._record_publish(topic, data, attributes)
        url = self._forward_map.get(topic)
        if not url:
            return False
        return await self._forward_to_http(url, msg_id, data, attributes)

    async def _forward_to_http(self, url: str, msg_id: str, data: bytes, attributes: dict[str, str]) -> bool:
        """POST a Pub/Sub-style envelope to a local HTTP endpoint. Returns True only on a 2xx response.

        Real Pub/Sub push retries a delivery that never reached the handler,
        so a connection failure (the target service is still booting) is
        retried here too. A non-2xx *response* is reported at WARNING and not
        retried — the handler already consumed the message.
        """
        import httpx

        envelope = {
            "message": {
                "data": base64.b64encode(data).decode("utf-8"),
                "attributes": attributes,
                "messageId": msg_id,
            },
            "subscription": "local-dev",
        }
        headers = {"Authorization": f"Bearer {self._forward_token}"} if self._forward_token else {}
        for attempt in range(1, _FORWARD_RETRIES + 1):
            try:
                # trust_env=False: never read HTTP(S)_PROXY / NO_PROXY from the environment for this
                # loopback call. A configured proxy would otherwise receive the internal push token.
                async with httpx.AsyncClient(trust_env=False) as client:
                    resp = await client.post(url, json=envelope, headers=headers, timeout=_FORWARD_TIMEOUT_SECONDS)
                if resp.status_code >= 400:
                    logger.warning("HTTP forward of %s to %s returned %d", msg_id, url, resp.status_code)
                    return False
                logger.debug("Forwarded %s to %s (status=%d)", msg_id, url, resp.status_code)
                return True
            except Exception as exc:
                if attempt >= _FORWARD_RETRIES:
                    logger.warning("HTTP forward failed for %s -> %s: %s", msg_id, url, exc)
                    return False
                logger.debug(
                    "HTTP forward attempt %d/%d for %s -> %s failed: %s",
                    attempt,
                    _FORWARD_RETRIES,
                    msg_id,
                    url,
                    exc,
                )
                await asyncio.sleep(_FORWARD_RETRY_BACKOFF_SECONDS * attempt)
        return False

    async def pull_dlq(
        self,
        subscription_name: str,
        max_messages: int = 10,
    ) -> list[dict[str, Any]]:
        """Return an empty list — the in-memory broker has no DLQ concept.

        Local dev does not dead-letter messages; failed handlers surface
        as exceptions in the same process.  Returning an empty list lets
        ``check_dlq_handler`` run in local mode without special-casing
        the provider.
        """
        return []

    def subscribe(self, topic: str, callback: Callable[..., Any]) -> None:
        """Register a callback to be invoked synchronously on publish."""
        self._subscribers[topic].append(callback)

    def get_messages(self, topic: str) -> list[dict[str, Any]]:
        """Return the retained messages published to a topic (for test inspection)."""
        return list(self._messages[topic])

    def clear(self) -> None:
        """Clear all stored messages and subscribers."""
        self._messages.clear()
        self._subscribers.clear()
