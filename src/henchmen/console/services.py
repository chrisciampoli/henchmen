"""Live state of the services ``henchmen serve`` runs, for ``/console/api/status``.

The combined app's lifespan updates a :class:`ServiceHealth`; the Console reads
its snapshot. When a service's lifespan raises during startup the error is
kept, so ``henchmen serve`` can fall back to the needs-attention Console
instead of exiting.
"""

from __future__ import annotations

from enum import StrEnum

SERVICE_NAMES: tuple[str, ...] = ("dispatch", "mastermind", "forge")


class ServiceState(StrEnum):
    """Lifecycle state of one service."""

    OFF = "off"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAILED = "failed"


class ServiceHealth:
    """Mutable per-process record of each service's state and the first startup error."""

    def __init__(self) -> None:
        self._states: dict[str, ServiceState] = dict.fromkeys(SERVICE_NAMES, ServiceState.OFF)
        self.startup_error: Exception | None = None

    def set_all(self, state: ServiceState) -> None:
        """Put every service in ``state``."""
        for name in self._states:
            self._states[name] = state

    def record_startup_failure(self, name: str, error: Exception) -> None:
        """Mark ``name`` failed and keep the first startup error."""
        self._states[name] = ServiceState.FAILED
        if self.startup_error is None:
            self.startup_error = error

    def finish(self) -> None:
        """After shutdown (or a failed startup): every service that did not fail is off."""
        for name, state in self._states.items():
            if state != ServiceState.FAILED:
                self._states[name] = ServiceState.OFF

    def snapshot(self) -> dict[str, str]:
        """``{"dispatch": "running", ...}`` for the status route."""
        return {name: state.value for name, state in self._states.items()}
