"""Security posture: may a development-only fail-open path be taken?

Several components relax a check in ``HENCHMEN_ENVIRONMENT=dev`` so a
repository checkout works without cloud credentials (unauthenticated Pub/Sub
pushes, an open task API, unsigned webhooks, open ``/metrics``, CI without a
GitHub token, an operative without a document store, a simulated lair pass).
The local image also runs with ``dev``, but a desktop install is a real
installation, so every one of those paths asks :func:`fail_open_allowed`
instead of comparing the environment directly.

An operative container launched by a desktop install has no data directory;
it is recognised by the task-scoped token its Lair injects
(``HENCHMEN_OPERATIVE_TASK_TOKEN``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from henchmen.config.paths import is_desktop_install
from henchmen.config.settings import Environment

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


def is_desktop_posture(settings: Settings) -> bool:
    """True for a desktop install and for any operative it launched."""
    token = getattr(settings, "operative_task_token", "")
    return is_desktop_install() or (isinstance(token, str) and bool(token.strip()))


def fail_open_allowed(settings: Settings) -> bool:
    """True only in DEV on a repository checkout (never on a desktop install or its operatives)."""
    environment = getattr(settings.environment, "value", settings.environment)
    return environment == Environment.DEV.value and not is_desktop_posture(settings)
