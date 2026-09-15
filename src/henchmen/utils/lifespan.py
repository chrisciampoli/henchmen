"""Shutdown guard shared by every FastAPI lifespan ``henchmen serve`` combines.

A sub-app entered after another one can fail to start; the combined app's
``AsyncExitStack`` then unwinds the earlier lifespans by throwing that
exception in at their ``yield``. Their shutdown runs from ``finally`` and must
never replace that original exception — not with an ``Exception``, and not
with a ``CancelledError`` (or any other ``BaseException``) raised while
shutting down.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def run_shutdown(name: str, shutdown: Callable[[], Awaitable[None]], *, original: BaseException | None) -> None:
    """Await ``shutdown``; never let what it raises mask ``original``.

    * An ``Exception`` from shutdown is logged and swallowed, as before.
    * Any other ``BaseException`` (``CancelledError``, ``KeyboardInterrupt``) is
      logged and swallowed only while ``original`` is propagating — the caller's
      ``finally`` then lets ``original`` continue. With nothing propagating it
      is re-raised, so a cancellation during a clean shutdown is still honoured.
    """
    try:
        await shutdown()
    except Exception:
        logger.warning("[%s] Shutdown raised", name, exc_info=True)
    except BaseException:
        logger.warning("[%s] Shutdown was interrupted", name, exc_info=True)
        if original is None:
            raise


__all__ = ["run_shutdown"]
