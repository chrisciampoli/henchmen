"""Date arithmetic helpers used as a bugfix fixture.

BUG: ``timedelta`` is referenced but never imported, so simply importing
this module raises ``NameError`` on Python 3.12. The fix is to add
``from datetime import timedelta`` (or import ``datetime`` and reference
``datetime.timedelta``) so both functions resolve cleanly.
"""

from __future__ import annotations

from datetime import datetime


def days_from_now(n: int) -> datetime:
    """Return a ``datetime`` that is ``n`` days in the future."""
    return datetime.now() + timedelta(days=n)  # noqa: F821 — missing import is the bug


def weeks_from_now(n: int) -> datetime:
    """Return a ``datetime`` that is ``n`` weeks in the future."""
    return datetime.now() + timedelta(weeks=n)  # noqa: F821 — missing import is the bug
