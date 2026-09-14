"""Access to optional provider settings that ``Settings`` may not declare yet.

A few provider knobs (the Cloud Build builder image, the ECS execution role,
local storage locations) are read before a matching ``Settings`` field exists
in every deployment. Reading them through one helper keeps the fallback
behaviour identical across providers: a missing field, a non-string value or
a blank string all mean "unset", and the caller supplies the default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from henchmen.config.settings import Settings


def optional_setting(settings: Settings, name: str) -> str:
    """Return the stripped string value of ``settings.<name>``, or ``""`` when unset."""
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


__all__ = ["optional_setting"]
