"""Shared Pydantic v2 base class for Henchmen domain models.

Every model that represents a Henchmen data contract should inherit from
``StrictBase`` instead of ``BaseModel``. The strict config:

- ``extra="forbid"`` — reject unknown fields so schema drift is caught at the
  boundary (bad dispatcher payloads, stale Firestore documents, etc.) instead
  of silently discarded.
- ``validate_assignment=True`` — re-validate on attribute writes so mutating
  a loaded model (``task.status = "completed"``) can't bypass type constraints.
- ``str_strip_whitespace=False`` — deliberately explicit: we do *not* silently
  mutate strings. Whitespace is preserved as-is.

Models that must accept unknown fields (e.g. wire-format webhook payloads
from third parties) should inherit directly from ``BaseModel`` and leave a
comment explaining why.
"""

from pydantic import BaseModel, ConfigDict


class StrictBase(BaseModel):
    """Strict base for Henchmen domain models."""

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=False,
        # Several Henchmen models legitimately use ``model_name`` /
        # ``model_calls`` field names — these describe LLM models, not
        # Pydantic models. Opt out of Pydantic's ``model_`` namespace
        # protection so StrictBase doesn't emit warnings or raise.
        protected_namespaces=(),
    )
